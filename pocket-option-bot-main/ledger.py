"""SQLite-backed payment ledger with idempotent provider-event ingestion."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TransactionLedger:
    def __init__(self, database_path: str = "transactions.sqlite3") -> None:
        self.database_path = database_path
        parent = Path(database_path).parent
        if str(parent) != ".":
            parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS payment_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    provider_event_id TEXT NOT NULL,
                    reference TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0),
                    currency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    account_reference TEXT,
                    occurred_at TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(provider, provider_event_id)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_payment_reference ON payment_transactions(reference)")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path)
        db.row_factory = sqlite3.Row
        return db

    def record(self, *, provider: str, provider_event_id: str, reference: str,
               amount_minor: int, currency: str, status: str,
               account_reference: str | None = None, occurred_at: str | None = None,
               payload: dict[str, Any] | None = None) -> bool:
        if not all((provider, provider_event_id, reference, currency, status)):
            raise ValueError("provider, event id, reference, currency and status are required")
        if amount_minor < 0:
            raise ValueError("amount_minor cannot be negative")
        try:
            with self._connect() as db:
                db.execute("""INSERT INTO payment_transactions
                    (provider, provider_event_id, reference, amount_minor, currency, status,
                     account_reference, occurred_at, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (provider, provider_event_id, reference, amount_minor, currency.upper(),
                     status.lower(), account_reference, occurred_at,
                     json.dumps(payload or {}, separators=(",", ":"), sort_keys=True),
                     datetime.now(timezone.utc).isoformat()))
            return True
        except sqlite3.IntegrityError:
            return False

    def list_transactions(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as db:
            rows = db.execute("SELECT * FROM payment_transactions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            items.append(item)
        return items

    def reconcile(self, *, reference: str, amount_minor: int, currency: str) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute("SELECT id, provider, reference, amount_minor, currency, status "
                              "FROM payment_transactions WHERE reference=? ORDER BY id DESC",
                              (reference,)).fetchall()
        candidates = [dict(row) for row in rows]
        exact = [r for r in candidates if r["amount_minor"] == amount_minor
                 and r["currency"] == currency.upper()
                 and r["status"] in {"success", "completed", "paid"}]
        if len(exact) == 1:
            return {"status": "matched", "transaction": exact[0]}
        if len(exact) > 1:
            return {"status": "ambiguous", "matches": exact}
        if candidates:
            return {"status": "reference_found_but_details_differ", "matches": candidates}
        return {"status": "unmatched", "matches": []}
