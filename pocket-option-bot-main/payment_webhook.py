"""Provider-neutral signed payment webhook ingestion.

Map a provider's official webhook fields into the canonical event shape before
calling PaymentWebhookProcessor. Never treat an unsigned callback as proof of payment.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from transaction_store import TransactionStore


@dataclass(frozen=True)
class PaymentEvent:
    event_id: str
    reference: str
    amount_minor: int
    currency: str
    status: str
    occurred_at: str | None = None
    account_reference: str | None = None


class PaymentWebhookProcessor:
    def __init__(self, store: TransactionStore, provider: str, secret: str) -> None:
        if not provider or not secret:
            raise ValueError("provider and webhook secret are required")
        self.store, self.provider, self.secret = store, provider, secret.encode("utf-8")

    def verify_signature(self, raw_body: bytes, signature: str) -> bool:
        """Verify an HMAC-SHA256 hex digest. Provider adapters may override this."""
        supplied = signature.removeprefix("sha256=").strip().lower()
        expected = hmac.new(self.secret, raw_body, hashlib.sha256).hexdigest()
        return bool(supplied) and hmac.compare_digest(expected, supplied)

    def process(self, raw_body: bytes, signature: str, event: PaymentEvent) -> dict[str, Any]:
        if not self.verify_signature(raw_body, signature):
            raise PermissionError("Invalid payment webhook signature")
        if event.amount_minor < 0:
            raise ValueError("amount_minor cannot be negative")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Webhook body must be valid UTF-8 JSON") from exc
        inserted = self.store.record(
            provider=self.provider, provider_event_id=event.event_id,
            reference=event.reference, amount_minor=event.amount_minor,
            currency=event.currency, status=event.status,
            account_reference=event.account_reference, occurred_at=event.occurred_at,
            payload=payload if isinstance(payload, dict) else {"data": payload},
        )
        return {"accepted": True, "duplicate": not inserted, "event_id": event.event_id}
