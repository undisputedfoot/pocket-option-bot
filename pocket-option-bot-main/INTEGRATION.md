# Pocket Option account and payment integration

## What is included

- `account_monitor.py`: read-only session connection and balance-event request.
- `transaction_store.py`: SQLite transaction ledger, unique provider-event IDs, and reference/amount/currency reconciliation.
- `payment_webhook.py`: provider-neutral HMAC-SHA256 webhook processor. A provider adapter must map the provider's official webhook fields and signature scheme into `PaymentEvent`.
- Existing trade mirroring remains in `mirror_trade.py`.

## Install and configure

The current `pocket-option` SDK declares Python 3.13+ support. Use Python 3.13 or newer:

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set credentials locally. Do not commit `.env`, paste the session ID into chat, or put it in logs. The session is a bearer credential.

## Account balance monitor

`PocketOptionAccountMonitor` accepts the session, numeric UID, account mode, platform, and SDK region. It waits for an explicit authorization event, requests the documented balance update event, and waits for the balance response. It does not place, close, or modify trades.

Example (run locally after configuring a trusted secret source):

```python
import asyncio
import os
from account_monitor import PocketOptionAccountMonitor

async def main():
    monitor = PocketOptionAccountMonitor(
        os.environ["PO_SESSION"],
        uid=int(os.environ["PO_UID"]),
        is_demo=os.getenv("PO_IS_DEMO", "1") == "1",
        platform=int(os.getenv("PO_PLATFORM", "1")),
        region=os.getenv("PO_REGION", "DEMO"),
    )
    try:
        await monitor.connect()
        print(monitor.snapshot())
    finally:
        await monitor.close()

asyncio.run(main())
```

The SDK is unofficial and Pocket Option may change its private WebSocket protocol. Treat the returned balance as an observed account snapshot, not as a deposit/withdrawal ledger.

## Payment provider integration

This repository deliberately does not claim a live provider connection until the provider is selected and its official credentials and webhook signing format are configured. The included processor requires a verified signature and a canonical event with a stable provider event ID, reference, amount in minor units (for KES, cents), currency, and status.

A provider adapter must:
1. Follow the provider's official API and webhook documentation.
2. Verify the exact signature scheme and raw request bytes.
3. Map provider events to `PaymentEvent`.
4. Call `PaymentWebhookProcessor.process`.
5. Acknowledge duplicates safely; the SQLite unique constraint makes ingestion idempotent.

Never mark a payment as paid based only on a client redirect, screenshot, or unverified callback. Keep provider payment references distinct from Pocket Option trading/deal IDs. Reconciliation matches reference, amount, currency, and a successful status; ambiguous or mismatched records are surfaced rather than auto-approved.

## Tests

```bash
pytest -q
```

## Security notes

- The SSID/session token is sensitive. Store it in environment variables or a secret manager.
- Rotate/revoke the session if it is exposed.
- The example `.env` file is not for source control.
- Do not log raw webhook payloads in production if they contain personal/payment data.
- HTTPS is required for a deployed webhook endpoint; terminate TLS at a trusted reverse proxy or hosting provider.
