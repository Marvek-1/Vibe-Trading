"""Storage seam for the tradingview-mcp journal.

Backend is chosen once, at import time, by ``TV_MCP_DSN``:
present -> Postgres (canon, shared with the rest of the financial-ops
database), absent -> SQLite (standalone/laptop fallback). Both implement
the same ``JournalStore`` interface in ``base.py`` so ``server.py`` never
branches on backend.
"""

from __future__ import annotations

import os

from .base import JournalStore


def get_store() -> JournalStore:
    dsn = os.environ.get("TV_MCP_DSN")
    if dsn:
        from .postgres_store import PostgresJournalStore
        return PostgresJournalStore(dsn)
    from .sqlite_store import SQLiteJournalStore
    return SQLiteJournalStore()
