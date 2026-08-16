"""SQLite backend -- standalone/laptop fallback. Same behavior server.py had
inline before the storage seam existed; only moved, not changed.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from typing import Any, Optional

from .base import JournalStore

DB_PATH = os.environ.get(
    "TV_MCP_DB",
    os.path.join(os.path.expanduser("~"), ".tradingview_mcp", "journal.db"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id          TEXT PRIMARY KEY,
    ts_utc      REAL NOT NULL,
    event_type  TEXT NOT NULL CHECK (event_type IN ('SIGNAL','ENTRY','EXIT','NOTE')),
    symbol      TEXT NOT NULL,
    exchange    TEXT,
    side        TEXT CHECK (side IN ('LONG','SHORT') OR side IS NULL),
    price       REAL,
    qty         REAL,
    ref_id      TEXT,            -- links EXIT/NOTE back to an ENTRY/SIGNAL id
    detail      TEXT,            -- free text / JSON blob
    attested_by TEXT NOT NULL DEFAULT 'unattested'
);
CREATE INDEX IF NOT EXISTS idx_journal_symbol ON journal(symbol);
CREATE INDEX IF NOT EXISTS idx_journal_type   ON journal(event_type);

CREATE TABLE IF NOT EXISTS watchlist (
    symbol   TEXT NOT NULL,
    exchange TEXT NOT NULL,
    screener TEXT NOT NULL,
    added_ts REAL NOT NULL,
    PRIMARY KEY (symbol, exchange)
);
"""


class SQLiteJournalStore(JournalStore):
    def __init__(self, db_path: str = DB_PATH) -> None:
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)  # loud, unconditional, idempotent
        return conn

    def append(self, event: dict[str, Any]) -> str:
        event_id = str(uuid.uuid4())
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO journal (id, ts_utc, event_type, symbol, exchange, side, price, qty, ref_id, detail, attested_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, time.time(), event["event_type"], event["symbol"].upper(),
                 (event.get("exchange") or "").upper() or None, event.get("side"),
                 event.get("price"), event.get("qty"), event.get("ref_id"),
                 event.get("detail"), event.get("attested_by", "unattested")),
            )
            conn.commit()
        finally:
            conn.close()
        return event_id

    def list(self, *, symbol: Optional[str], event_type: Optional[str], limit: int) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM journal WHERE 1=1", []
        if symbol:
            q += " AND symbol = ?"; args.append(symbol.upper())
        if event_type:
            q += " AND event_type = ?"; args.append(event_type)
        q += " ORDER BY ts_utc DESC LIMIT ?"; args.append(limit)
        conn = self._conn()
        try:
            return [dict(r) for r in conn.execute(q, args).fetchall()]
        finally:
            conn.close()

    def resolved_pairs(self, *, symbol: Optional[str], since_ts: float) -> list[dict[str, Any]]:
        sym_clause, args = ("AND e.symbol = ? ", [symbol.upper()]) if symbol else ("", [])
        args.append(since_ts)
        conn = self._conn()
        try:
            rows = conn.execute(f"""
                SELECT e.side, e.price AS entry_px, x.price AS exit_px,
                       COALESCE(e.qty, 1.0) AS qty, e.ts_utc AS entry_ts, x.ts_utc AS exit_ts
                FROM journal e JOIN journal x ON x.ref_id = e.id
                WHERE e.event_type='ENTRY' AND x.event_type='EXIT'
                  AND e.price IS NOT NULL AND x.price IS NOT NULL {sym_clause}
                  AND e.ts_utc >= ?
            """, args).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def event_counts(self, *, symbol: Optional[str], since_ts: float) -> dict[str, int]:
        sym_clause, args = ("AND symbol = ? ", [symbol.upper()]) if symbol else ("", [])
        conn = self._conn()
        try:
            rows = conn.execute(
                f"SELECT event_type, COUNT(*) c FROM journal WHERE ts_utc >= ? {sym_clause}GROUP BY event_type",
                [since_ts] + args,
            ).fetchall()
            return {r["event_type"]: r["c"] for r in rows}
        finally:
            conn.close()

    def watchlist_add(self, *, symbol: str, exchange: str, screener: str) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO watchlist (symbol, exchange, screener, added_ts) VALUES (?,?,?,?)",
                (symbol.upper(), exchange.upper(), screener.lower(), time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def watchlist_list(self) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT symbol, exchange, screener FROM watchlist ORDER BY symbol").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
