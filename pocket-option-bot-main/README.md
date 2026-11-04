# Pocket Option Bot

Trade mirroring utilities and account/payment integration components.

See [INTEGRATION.md](INTEGRATION.md) for setup, account balance monitoring, transaction storage, webhook processing, and reconciliation.

## Existing mirror bot

The original `mirror_trade.py` functionality is retained. Configure `MASTER_SSID` and `CHILD_SSID` locally in `.env` or environment variables before running it.

## Security

Never commit `.env` or expose Pocket Option session IDs (SSID). The account monitor is read-only; payment webhook ingestion requires a valid signature. The Pocket Option SDK is unofficial and its private protocol may change.
