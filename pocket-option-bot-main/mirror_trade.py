import asyncio
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from logger import setup_logger

logger = setup_logger("mirror_trade", log_file=str(Path(__file__).resolve().parent / "mirror_trade.log"))


class PocketOptionTradeExecutor:
    """Minimal executor that opens a trade using a compatible Pocket Option client."""

    def __init__(self, ssid: str, label: str, region: str | None = None, is_demo: int = 1):
        if not ssid:
            raise ValueError("SSID is required")

        self.raw_ssid = ssid.strip()
        self.ssid, self.auth_payload = self._extract_ssid_payload(self.raw_ssid)
        self.label = label
        self.region = region
        self.is_demo = is_demo
        self._client: Any | None = None
        self._client_type: str | None = None
        self._http_session: Any | None = None
        self._wait_task: asyncio.Task | None = None
        self._opened_deals_snapshot: list[Any] = []
        self._opened_deals_event: asyncio.Event | None = None

    def _extract_ssid_payload(self, ssid: str) -> tuple[str, dict[str, Any]]:
        if not ssid:
            return ssid, {}

        match = re.search(r'\["auth",(\{.*\})\]\s*$', ssid)
        if match:
            try:
                payload = json.loads(match.group(1))
                return payload.get("session", ssid), payload
            except json.JSONDecodeError:
                pass

        return ssid, {}

    def _build_legacy_client(self) -> Any:
        try:
            from pocketoptionapi import PocketOption

            return PocketOption(ssid=self.ssid)
        except ImportError:
            pass

        try:
            from pocket_option_api import PocketOption

            return PocketOption(ssid=self.ssid)
        except ImportError:
            pass

        raise ImportError("Legacy Pocket Option client packages are not installed")

    def _build_async_client(self) -> Any:
        try:
            from pocket_option import PocketOptionClient

            return PocketOptionClient(logger=False, http_session=self._build_http_session())
        except ImportError as exc:
            raise RuntimeError(
                "A compatible Pocket Option package is required. Install it with: pip install pocket-option>=0.3.0"
            ) from exc

    def _build_http_session(self) -> Any:
        try:
            import aiohttp
            from aiohttp.resolver import ThreadedResolver
        except ImportError as exc:
            raise RuntimeError(
                "aiohttp is required for the Pocket Option client. Install it with: pip install aiohttp"
            ) from exc

        self._http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=ThreadedResolver()))
        return self._http_session

    def _resolve_region(self) -> str:
        default_region = "DEMO" if self._resolve_is_demo() == 1 else "EUROPA"
        region_value = self.region or os.getenv("POCKET_OPTION_REGION", default_region)
        region_value = region_value.strip().upper()

        try:
            from pocket_option.constants import Regions

            region = getattr(Regions, region_value, None)
            return region.value if region is not None else Regions.DEMO.value
        except Exception:
            return "wss://demo-api-eu.po.market" if default_region == "DEMO" else "wss://api-eu.po.market"

    def _resolve_is_demo(self) -> int:
        value = self.auth_payload.get("isDemo", self.auth_payload.get("is_demo", self.is_demo))
        if isinstance(value, bool):
            return 1 if value else 0
        try:
            return 1 if int(value) else 0
        except (TypeError, ValueError):
            return 1

    def _build_authorization_data(self) -> Any:
        from pocket_option.models import AuthorizationData

        return AuthorizationData(
            session=self.ssid,
            isDemo=self._resolve_is_demo(),
            uid=self.auth_payload.get("uid", 0),
            platform=self.auth_payload.get("platform", 1),
            isFastHistory=self.auth_payload.get("isFastHistory", False),
            isOptimized=self.auth_payload.get("isOptimized", False),
        )

    async def ensure_connected(self) -> Any:
        if self._client is not None:
            return self._client

        try:
            self._client = self._build_async_client()
            self._client_type = "async"
            logger.info("%s | using async pocket-option client", self.label)
            await self._connect_async_client()
            return self._client
        except RuntimeError:
            pass

        self._client = self._build_legacy_client()
        self._client_type = "legacy"
        logger.info("%s | using legacy Pocket Option client", self.label)
        return self._client

    async def _connect_async_client(self) -> None:
        from pocket_option.contrib.default_init import default_init

        client = self._client
        if client is None:
            raise RuntimeError("Async Pocket Option client is not initialized")

        default_init(client, authorization=self._build_authorization_data())
        self._register_async_deal_handlers()
        server_url = self._resolve_region()
        logger.info("%s | connecting to Pocket Option server %s", self.label, server_url)
        await client.connect(server_url)

        try:
            await asyncio.wait_for(client.authorized_event.wait(), timeout=10)
            logger.info("%s | authorized successfully", self.label)
        except asyncio.TimeoutError:
            logger.warning("%s | authorization timed out", self.label)

        self._wait_task = asyncio.create_task(client.wait())

    def _register_async_deal_handlers(self) -> None:
        client = self._client
        if client is None:
            return

        client.add_on("successopenOrder", self._handle_success_open_deal, model=None)
        client.add_on("updateOpenedDeals", self._handle_opened_deals_update, model=None)
        client.add_on("successcloseOrder", self._handle_success_close_deals, model=None)

    def _get_opened_deals_event(self) -> asyncio.Event:
        if self._opened_deals_event is None:
            self._opened_deals_event = asyncio.Event()
        return self._opened_deals_event

    async def _handle_success_open_deal(self, deal: Any) -> None:
        self._remember_open_deal(deal)
        try:
            payload = _deal_to_trade_event(deal)
        except Exception:
            payload = _serialize_deal(deal)
        logger.info("%s | observed opened deal | %s", self.label, json.dumps(payload, default=str))

    async def _handle_opened_deals_update(self, deals: list[Any] | None) -> None:
        self._opened_deals_snapshot = list(deals or [])
        self._get_opened_deals_event().set()
        logger.info("%s | opened deals snapshot updated | count=%s", self.label, len(self._opened_deals_snapshot))

    async def _handle_success_close_deals(self, close_event: Any) -> None:
        closed_deals = _get_deal_value(close_event, "deals") or []
        closed_ids = {_extract_trade_id(deal) for deal in closed_deals}
        self._opened_deals_snapshot = [
            deal for deal in self._opened_deals_snapshot if _extract_trade_id(deal) not in closed_ids
        ]

    def _remember_open_deal(self, deal: Any) -> None:
        trade_id = _extract_trade_id(deal)
        if not trade_id:
            self._opened_deals_snapshot.append(deal)
            return

        for index, existing in enumerate(self._opened_deals_snapshot):
            if _extract_trade_id(existing) == trade_id:
                self._opened_deals_snapshot[index] = deal
                return
        self._opened_deals_snapshot.append(deal)

    async def place_trade(self, asset: str, action: str, amount: float, duration: int) -> dict[str, Any]:
        client = await self.ensure_connected()
        norm_action = normalize_action(action)
        order_amount = int(float(amount))
        order_duration = int(duration)
        logger.info(
            "%s | opening trade | asset=%s | action=%s | amount=%.2f | duration=%s",
            self.label,
            asset,
            norm_action,
            amount,
            duration,
        )

        if self._client_type == "legacy":
            trade_method = getattr(client, "buy", None) if norm_action == "call" else getattr(client, "sell", None)
            if trade_method is None:
                logger.error("%s | unsupported legacy trade method", self.label)
                raise RuntimeError(f"The Pocket Option client does not expose a supported trade method for {self.label}")

            trade_id, deal = trade_method(asset=asset, amount=order_amount, time=order_duration)
            return {
                "label": self.label,
                "ssid": self.ssid,
                "asset": asset,
                "action": norm_action,
                "amount": order_amount,
                "duration": order_duration,
                "trade_id": trade_id,
                "deal": deal,
            }

        from pocket_option.models import Asset, DealAction

        if not hasattr(client, "deals") or not hasattr(client.deals, "open_deal"):
            raise RuntimeError("Async Pocket Option client does not support opening deals")

        asset_enum = Asset(asset)
        deal_action = DealAction.CALL if norm_action == "call" else DealAction.PUT
        deal = await client.deals.open_deal(
            asset=asset_enum,
            amount=order_amount,
            action=deal_action,
            time=order_duration,
            is_demo=self._resolve_is_demo(),
        )
        self._remember_open_deal(deal)

        result = {
            "label": self.label,
            "ssid": self.ssid,
            "asset": asset,
            "action": norm_action,
            "amount": order_amount,
            "duration": order_duration,
            "trade_id": str(getattr(deal, "id", "")),
            "deal": deal,
        }
        logger.info("%s | trade accepted | %s", self.label, json.dumps(result, default=str))
        return result

    async def refresh_open_deals(self, timeout: float = 0.5) -> list[Any]:
        client = await self.ensure_connected()

        if self._client_type == "legacy":
            if hasattr(client, "get_open_deals"):
                return client.get_open_deals() or []
            return []

        if not hasattr(client, "deals") or client.deals is None:
            return []

        if hasattr(client, "emit") and hasattr(client.emit, "deals_update_opened"):
            update_event = self._get_opened_deals_event()
            update_event.clear()
            await client.emit.deals_update_opened()
            try:
                await asyncio.wait_for(update_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.debug("%s | no opened deals snapshot response before poll timeout", self.label)
            return self._opened_deals_snapshot.copy()

        open_deals = await client.deals.get_deals(query=None)
        open_deals = list(open_deals)
        for deal in open_deals:
            logger.debug(
                "%s | fetched open deal payload | %s",
                self.label,
                json.dumps(_serialize_deal(deal), default=str),
            )
        return open_deals

    async def get_open_deals(self, *, refresh: bool = True) -> list[Any]:
        if refresh:
            return await self.refresh_open_deals()

        client = await self.ensure_connected()
        if self._client_type == "async":
            return self._opened_deals_snapshot.copy()

        if hasattr(client, "get_open_deals"):
            return client.get_open_deals() or []
        return []


def normalize_action(action: Any) -> str:
    if hasattr(action, "value") and not isinstance(action, str):
        return normalize_action(action.value)

    if isinstance(action, bool):
        return "call" if action is False else "put"

    if isinstance(action, int):
        if action == 0:
            return "call"
        if action == 1:
            return "put"

    value = "" if action is None else str(action).strip().lower()
    if "." in value:
        value = value.rsplit(".", 1)[-1]

    if value in {"call", "buy", "up", "rise", "higher", "0"}:
        return "call"
    if value in {"put", "sell", "down", "fall", "lower", "1"}:
        return "put"

    raise ValueError(f"Unsupported action: {action}")


def normalize_asset(asset: Any) -> str:
    if hasattr(asset, "value") and not isinstance(asset, str):
        value = getattr(asset, "value")
        if value:
            return str(value)

    value = "" if asset is None else str(asset).strip()
    if value.startswith("Asset."):
        return value.rsplit(".", 1)[-1]
    return value


def _get_deal_value(deal: Any, *names: str) -> Any:
    if deal is None:
        return None

    if isinstance(deal, dict):
        for name in names:
            aliases = {name, _snake_to_camel(name)}
            for alias in aliases:
                if alias in deal and deal[alias] is not None:
                    return deal[alias]
        return None

    for name in names:
        if hasattr(deal, name):
            value = getattr(deal, name)
            if not callable(value) and value is not None:
                return value
    return None


def _snake_to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.timestamp()
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time()).timestamp()
    return _to_float(value)


def _extract_duration(deal: Any) -> int | None:
    for name in ("time", "duration", "timeframe", "expiration", "expiry", "period"):
        value = _to_float(_get_deal_value(deal, name))
        if value and value > 0:
            return int(round(value))

    open_timestamp = _to_timestamp(_get_deal_value(deal, "open_timestamp", "openTime", "open_time"))
    close_timestamp = _to_timestamp(_get_deal_value(deal, "close_timestamp", "closeTime", "close_time"))
    if open_timestamp is not None and close_timestamp is not None and close_timestamp > open_timestamp:
        return int(round(close_timestamp - open_timestamp))

    return None


def _extract_trade_id(deal: Any) -> str:
    for name in ("id", "trade_id", "ticket", "copy_ticket", "request_id"):
        value = _get_deal_value(deal, name)
        if value:
            return str(value)

    raw = _serialize_deal(deal)
    fallback_parts = [
        raw.get("uid"),
        raw.get("asset"),
        raw.get("command"),
        raw.get("amount"),
        raw.get("open_timestamp") or raw.get("openTimestamp"),
        raw.get("open_price") or raw.get("openPrice"),
    ]
    return "|".join(str(part) for part in fallback_parts if part is not None)


def _deal_open_sort_key(deal: Any) -> tuple[float, str]:
    open_timestamp = _to_timestamp(_get_deal_value(deal, "open_timestamp", "openTime", "open_time")) or 0.0
    return (open_timestamp, _extract_trade_id(deal))


def _deal_to_trade_event(deal: Any) -> dict[str, Any]:
    raw = _serialize_deal(deal)
    asset = normalize_asset(_get_deal_value(deal, "asset", "symbol"))
    action = normalize_action(_get_deal_value(deal, "command", "action", "direction"))
    amount = _to_float(_get_deal_value(deal, "amount", "amount_usd", "amountUSD"))
    duration = _extract_duration(deal)
    trade_id = _extract_trade_id(deal)

    return {
        "trade_id": trade_id,
        "asset": asset,
        "action": action,
        "amount": amount,
        "duration": duration,
        "raw": raw,
    }


def _default_amount() -> float:
    return _to_float(os.getenv("AMOUNT") or load_config().get("AMOUNT")) or 1.0


def _default_duration() -> int:
    value = _to_float(os.getenv("DURATION") or load_config().get("DURATION"))
    return int(value) if value and value > 0 else 60


def _serialize_deal(deal: Any) -> dict[str, Any]:
    if hasattr(deal, "model_dump"):
        try:
            return deal.model_dump(mode="json")
        except Exception:
            pass

    serialized: dict[str, Any] = {}
    for attr in sorted(set(dir(deal)) - set(dir(object))):
        if attr.startswith("_"):
            continue
        try:
            value = getattr(deal, attr)
            if callable(value):
                continue
            serialized[attr] = value
        except Exception:
            serialized[attr] = "<error>"
    return serialized


def create_trade_plan(asset: str, action: str, amount: float, duration: int) -> dict[str, dict[str, Any]]:
    normalized_action = normalize_action(action)
    return {
        "master": {
            "asset": asset,
            "action": normalized_action,
            "amount": amount,
            "duration": duration,
            "origin": "manual",
            "label": "master",
        },
        "child": {
            "asset": asset,
            "action": normalized_action,
            "amount": amount,
            "duration": duration,
            "origin": "mirrored_from_master",
            "label": "child",
            "triggered_by": "manual_master",
            "source_label": "master",
        },
    }


def execute_trade_plan(master_executor: Any, child_executor: Any, plan: dict[str, dict[str, Any]]) -> dict[str, Any]:
    logger.info("mirror mode | starting instant master->child execution")
    master_result = master_executor.place_trade(
        plan["master"]["asset"],
        plan["master"]["action"],
        plan["master"]["amount"],
        plan["master"]["duration"],
    )
    child_result = child_executor.place_trade(
        plan["child"]["asset"],
        plan["child"]["action"],
        plan["child"]["amount"],
        plan["child"]["duration"],
    )

    result = {
        "master": master_result,
        "child": child_result,
    }
    logger.info("mirror mode | complete | %s", json.dumps(result, default=str))
    return result


async def execute_child_trade_plan(master_executor: Any, child_executor: Any, plan: dict[str, dict[str, Any]]) -> dict[str, Any]:
    logger.info("mirror mode | placing child trade for master event")
    child_result = await child_executor.place_trade(
        plan["child"]["asset"],
        plan["child"]["action"],
        plan["child"]["amount"],
        plan["child"]["duration"],
    )
    return {
        "master": plan["master"],
        "child": child_result,
    }


def load_config() -> dict[str, str]:
    config: dict[str, str] = {}
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip().strip('"').strip("'")

    for key in ["MASTER_SSID", "CHILD_SSID"]:
        env_value = os.getenv(key)
        if env_value is not None:
            config[key] = env_value

    return config


def main() -> None:
    config = load_config()
    master_ssid = config.get("MASTER_SSID") or os.getenv("MASTER_SSID")
    child_ssid = config.get("CHILD_SSID") or os.getenv("CHILD_SSID")
    if not master_ssid or not child_ssid:
        raise SystemExit("Set MASTER_SSID and CHILD_SSID in your environment or .env file before running this script")

    logger.info("mirror mode enabled | master=%s | child=%s", master_ssid[:8], child_ssid[:8])
    print("One-way mirror mode enabled: the child account will mirror the master account trade automatically.")

    master_executor = PocketOptionTradeExecutor(master_ssid, "master")
    child_executor = PocketOptionTradeExecutor(child_ssid, "child")

    try:
        asyncio.run(run_mirror_loop(master_executor, child_executor))
    except KeyboardInterrupt:
        logger.info("mirror mode stopped by user")


async def run_mirror_loop(master_executor: PocketOptionTradeExecutor, child_executor: PocketOptionTradeExecutor) -> None:
    logger.info("mirror loop started")
    await asyncio.gather(master_executor.ensure_connected(), child_executor.ensure_connected())
    seen_trade_ids: set[str] = set()

    mirror_existing = (os.getenv("MIRROR_EXISTING_OPEN_DEALS") or load_config().get("MIRROR_EXISTING_OPEN_DEALS") or "")
    if mirror_existing.strip().lower() not in {"1", "true", "yes", "on"}:
        existing_deals = await master_executor.get_open_deals()
        seen_trade_ids.update(_extract_trade_id(deal) for deal in existing_deals if _extract_trade_id(deal))
        logger.info("mirror loop baseline set | existing_open_deals=%s", len(seen_trade_ids))

    while True:
        try:
            trade_events = await detect_master_trade_events(master_executor, seen_trade_ids)
        except Exception as exc:
            logger.exception("mirror loop error: %s", exc)
            await asyncio.sleep(1)
            continue

        if not trade_events:
            await asyncio.sleep(1)
            continue

        for trade_event in trade_events:
            trade_id = trade_event.get("trade_id")
            if trade_id:
                seen_trade_ids.add(str(trade_id))

            logger.info("master trade detected | %s", json.dumps(trade_event, default=str))
            try:
                mirrored = await mirror_trade_to_child(master_executor, child_executor, trade_event)
                logger.info("child trade mirrored | %s", json.dumps(mirrored, default=str))
            except Exception as exc:
                logger.exception("child trade mirror failed: %s", exc)

        await asyncio.sleep(1)


async def detect_master_trade_event(master_executor: PocketOptionTradeExecutor, last_seen_trade_id: str | None) -> dict[str, Any] | None:
    seen_trade_ids = {last_seen_trade_id} if last_seen_trade_id else set()
    trade_events = await detect_master_trade_events(master_executor, seen_trade_ids)
    return trade_events[-1] if trade_events else None


async def detect_master_trade_events(
    master_executor: PocketOptionTradeExecutor,
    seen_trade_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    seen_trade_ids = seen_trade_ids or set()
    try:
        deals = await master_executor.get_open_deals()
    except Exception as exc:
        logger.warning("master deal polling failed: %s", exc)
        return []

    if not deals:
        return []

    trade_events: list[dict[str, Any]] = []
    for deal in sorted(deals, key=_deal_open_sort_key):
        trade_id = _extract_trade_id(deal)
        if trade_id and trade_id in seen_trade_ids:
            continue

        try:
            payload = _deal_to_trade_event(deal)
        except Exception as exc:
            logger.warning("unable to parse master deal event: %s | raw=%s", exc, json.dumps(_serialize_deal(deal), default=str))
            continue

        logger.info("master trade event payload | %s", json.dumps(payload, default=str))
        trade_events.append(payload)

    return trade_events


async def mirror_trade_to_child(master_executor: PocketOptionTradeExecutor, child_executor: PocketOptionTradeExecutor, trade_event: dict[str, Any]) -> dict[str, Any]:
    asset = normalize_asset(trade_event.get("asset"))
    if not asset:
        raise ValueError(f"Master trade event is missing asset: {trade_event}")

    action = normalize_action(trade_event.get("action"))
    amount = _to_float(trade_event.get("amount")) or _default_amount()
    duration_value = _to_float(trade_event.get("duration"))
    duration = int(duration_value) if duration_value and duration_value > 0 else _default_duration()

    plan = create_trade_plan(asset, action, amount, duration)
    logger.info("child mirror plan | %s", json.dumps(plan, default=str))
    return await execute_child_trade_plan(master_executor, child_executor, plan)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover - CLI entry point
        print(f"Mirror trade failed: {exc}", file=sys.stderr)
        sys.exit(1)
