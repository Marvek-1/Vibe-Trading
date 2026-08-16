"""Postgres backend -- canon storage, shared with the rest of the financial-ops
database (idim_ikang). Lives in its own ``journal`` schema (a data pocket, not
a separate database) so it never collides with idimikang's own tables.

The connecting role must have INSERT+SELECT only on journal.journal_events --
enforced by DB grant (see migrations/001_journal_events.sql), not by this
code. This module never issues UPDATE or DELETE against journal_events; there
is no method here that could.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

from .base import JournalStore


class PostgresJournalStore(JournalStore):
    def __init__(self, dsn: str, minconn: int = 1, maxconn: int = 2) -> None:
        self._pool = psycopg2.pool.ThreadedConnectionPool(minconn, maxconn, dsn)

    def _conn(self):
        return self._pool.getconn()

    def _put(self, conn) -> None:
        self._pool.putconn(conn)

    def append(self, event: dict[str, Any]) -> str:
        event_id = str(uuid.uuid4())
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO journal.journal_events "
                    "(id, ts_utc, event_type, symbol, exchange, side, price, qty, ref_id, detail, attested_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (event_id, time.time(), event["event_type"], event["symbol"].upper(),
                     (event.get("exchange") or "").upper() or None, event.get("side"),
                     event.get("price"), event.get("qty"), event.get("ref_id"),
                     event.get("detail"), event.get("attested_by", "unattested")),
                )
            conn.commit()
        finally:
            self._put(conn)
        return event_id

    def list(self, *, symbol: Optional[str], event_type: Optional[str], limit: int) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM journal.journal_events WHERE 1=1", []
        if symbol:
            q += " AND symbol = %s"; args.append(symbol.upper())
        if event_type:
            q += " AND event_type = %s"; args.append(event_type)
        q += " ORDER BY ts_utc DESC LIMIT %s"; args.append(limit)
        conn = self._conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(q, args)
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._put(conn)

    def resolved_pairs(self, *, symbol: Optional[str], since_ts: float) -> list[dict[str, Any]]:
        sym_clause, args = ("AND e.symbol = %s ", [symbol.upper()]) if symbol else ("", [])
        args.append(since_ts)
        conn = self._conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"""
                    SELECT e.side, e.price AS entry_px, x.price AS exit_px,
                           COALESCE(e.qty, 1.0) AS qty, e.ts_utc AS entry_ts, x.ts_utc AS exit_ts
                    FROM journal.journal_events e JOIN journal.journal_events x ON x.ref_id = e.id
                    WHERE e.event_type='ENTRY' AND x.event_type='EXIT'
                      AND e.price IS NOT NULL AND x.price IS NOT NULL {sym_clause}
                      AND e.ts_utc >= %s
                """, args)
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._put(conn)

    def event_counts(self, *, symbol: Optional[str], since_ts: float) -> dict[str, int]:
        sym_clause, args = ("AND symbol = %s ", [symbol.upper()]) if symbol else ("", [])
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT event_type, COUNT(*) c FROM journal.journal_events WHERE ts_utc >= %s {sym_clause}GROUP BY event_type",
                    [since_ts] + args,
                )
                return {row[0]: row[1] for row in cur.fetchall()}
        finally:
            self._put(conn)

    def watchlist_add(self, *, symbol: str, exchange: str, screener: str) -> None:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO journal.watchlist (symbol, exchange, screener, added_ts) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (symbol, exchange) DO NOTHING",
                    (symbol.upper(), exchange.upper(), screener.lower(), time.time()),
                )
            conn.commit()
        finally:
            self._put(conn)

    def watchlist_list(self) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT symbol, exchange, screener FROM journal.watchlist ORDER BY symbol")
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._put(conn)
