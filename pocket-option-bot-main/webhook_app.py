"""Small FastAPI endpoint for canonical, HMAC-signed payment webhooks.

Provider-specific adapters must map the provider's official event into this
canonical request contract and verify its actual signature scheme.
"""
from __future__ import annotations

import json
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Request

from account_monitor import load_account_config
from ledger import TransactionLedger
from payment_webhook import PaymentEvent, PaymentWebhookProcessor

app = FastAPI(title="Pocket Option Payment Reconciliation", version="1.0.0")


@lru_cache(maxsize=1)
def _processor() -> tuple[str, PaymentWebhookProcessor] | None:
    config = load_account_config()
    provider = config.get("PAYMENT_PROVIDER", "").strip()
    secret = config.get("PAYMENT_WEBHOOK_SECRET", "").strip()
    if not provider or provider == "your-provider" or not secret:
        return None
    database = config.get("PAYMENT_DATABASE", "transactions.sqlite3")
    return provider, PaymentWebhookProcessor(TransactionLedger(database), provider, secret)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/{provider}")
async def receive_payment_webhook(provider: str, request: Request) -> dict[str, object]:
    configured = _processor()
    if configured is None:
        raise HTTPException(status_code=503, detail="Payment provider webhook is not configured")
    expected_provider, processor = configured
    if provider != expected_provider:
        raise HTTPException(status_code=404, detail="Unknown payment provider")
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Webhook JSON must be an object")
        event = PaymentEvent(
            event_id=str(payload["event_id"]),
            reference=str(payload["reference"]),
            amount_minor=int(payload["amount_minor"]),
            currency=str(payload["currency"]),
            status=str(payload["status"]),
            occurred_at=str(payload["occurred_at"]) if payload.get("occurred_at") else None,
            account_reference=str(payload["account_reference"]) if payload.get("account_reference") else None,
        )
        return processor.process(raw_body, request.headers.get("X-Signature", ""), event)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid payment event: {exc}") from exc
