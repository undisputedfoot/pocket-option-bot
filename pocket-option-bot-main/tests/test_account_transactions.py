import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from payment_webhook import PaymentEvent, PaymentWebhookProcessor
from ledger import TransactionLedger


def test_transaction_store_deduplicates_provider_events(tmp_path):
    store = TransactionLedger(str(tmp_path / "payments.db"))
    args = dict(provider="test", provider_event_id="evt-1", reference="ref-1",
                amount_minor=12500, currency="KES", status="success")
    assert store.record(**args) is True
    assert store.record(**args) is False
    assert len(store.list_transactions()) == 1


def test_reconcile_exact_match(tmp_path):
    store = TransactionLedger(str(tmp_path / "payments.db"))
    store.record(provider="mpesa", provider_event_id="evt-2", reference="ABC123",
                 amount_minor=25000, currency="KES", status="completed")
    assert store.reconcile(reference="ABC123", amount_minor=25000, currency="KES")["status"] == "matched"


def test_reconcile_does_not_match_wrong_amount(tmp_path):
    store = TransactionLedger(str(tmp_path / "payments.db"))
    store.record(provider="mpesa", provider_event_id="evt-3", reference="ABC123",
                 amount_minor=25000, currency="KES", status="success")
    assert store.reconcile(reference="ABC123", amount_minor=30000, currency="KES")[
        "status"] == "reference_found_but_details_differ"


def test_webhook_signature_and_idempotency(tmp_path):
    secret = "local-test-secret"
    store = TransactionLedger(str(tmp_path / "payments.db"))
    processor = PaymentWebhookProcessor(store, "test-provider", secret)
    raw = json.dumps({"id": "evt-4", "paid": True}).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    event = PaymentEvent("evt-4", "PAY-44", 5000, "KES", "success")
    assert processor.process(raw, signature, event)["duplicate"] is False
    assert processor.process(raw, signature, event)["duplicate"] is True


def test_webhook_rejects_invalid_signature(tmp_path):
    processor = PaymentWebhookProcessor(TransactionLedger(str(tmp_path / "payments.db")),
                                         "test-provider", "secret")
    with pytest.raises(PermissionError):
        processor.process(b'{"id":"x"}', "bad", PaymentEvent("x", "r", 1, "KES", "success"))


def test_http_webhook_endpoint_accepts_signed_event_and_deduplicates(tmp_path, monkeypatch):
    import asyncio
    import webhook_app

    secret = "endpoint-secret"
    ledger = TransactionLedger(str(tmp_path / "endpoint.db"))
    processor = PaymentWebhookProcessor(ledger, "sample", secret)
    monkeypatch.setattr(webhook_app, "_processor", lambda: ("sample", processor))

    payload = {"event_id": "evt-http", "reference": "REF-HTTP",
               "amount_minor": 1250, "currency": "KES", "status": "success"}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

    class FakeRequest:
        headers = {"X-Signature": signature}

        async def body(self):
            return raw

    first = asyncio.run(webhook_app.receive_payment_webhook("sample", FakeRequest()))
    second = asyncio.run(webhook_app.receive_payment_webhook("sample", FakeRequest()))
    assert first["accepted"] is True
    assert first["duplicate"] is False
    assert second["duplicate"] is True
