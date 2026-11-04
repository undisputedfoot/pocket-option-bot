"""Read-only Pocket Option account/balance monitor using the existing SDK."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any


def load_account_config() -> dict[str, str]:
    """Read project .env and process environment without printing secrets."""
    values: dict[str, str] = {}
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    values.update({key: value for key, value in os.environ.items()
                   if key.startswith(("PO_", "PAYMENT_"))})
    return values



def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class PocketOptionAccountMonitor:
    """Read balance/account events; this class deliberately exposes no trade methods."""

    def __init__(self, session: str, *, uid: int, is_demo: bool = True,
                 platform: int = 1, region: str | None = None) -> None:
        if not session or not uid:
            raise ValueError("A session and numeric account UID are required")
        self._session = session
        self.uid, self.is_demo, self.platform = int(uid), bool(is_demo), int(platform)
        self.region = region
        self.client: Any | None = None
        self._balance_event = asyncio.Event()
        self._auth_event = asyncio.Event()
        self._balance: Any = None

    async def connect(self, timeout: float = 15.0) -> None:
        try:
            from pocket_option import PocketOptionClient
            from pocket_option.constants import Regions
            from pocket_option.contrib.default_init import default_init
            from pocket_option.models import AuthorizationData
        except ImportError as exc:
            raise RuntimeError("Install the pocket-option SDK (Python 3.13+)") from exc

        self.client = PocketOptionClient(logger=False)
        auth = AuthorizationData(
            session=self._session, isDemo=1 if self.is_demo else 0,
            uid=self.uid, platform=self.platform,
        )
        default_init(self.client, authorization=auth)
        self.client.add_on("successauth", self._on_authorized, model=None)
        self.client.add_on("successupdateBalance", self._on_balance, model=None)
        region_name = (self.region or ("DEMO" if self.is_demo else "EUROPA")).upper()
        region = getattr(Regions, region_name, None)
        if region is None:
            raise ValueError(f"Unknown Pocket Option region: {region_name}")
        await self.client.connect(region)
        try:
            await asyncio.wait_for(self._auth_event.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise TimeoutError("Pocket Option authorization was not confirmed") from exc
        if not hasattr(self.client, "emit") or not hasattr(self.client.emit, "update_balance"):
            raise RuntimeError("Installed SDK does not expose the documented update_balance event")
        await self.client.emit.update_balance()
        try:
            await asyncio.wait_for(self._balance_event.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("Pocket Option did not return a balance update") from exc

    async def _on_authorized(self, *_: Any) -> None:
        self._auth_event.set()

    async def _on_balance(self, payload: Any) -> None:
        self._balance = _plain(payload)
        self._balance_event.set()

    def snapshot(self) -> dict[str, Any]:
        if self._balance is None:
            raise RuntimeError("No balance has been received; connect() first")
        return {
            "connected": bool(self.client),
            "account_uid": self.uid,
            "account_mode": "demo" if self.is_demo else "real",
            "balance": self._balance,
        }

    async def close(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None
        self._session = ""
