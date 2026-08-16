"""JournalStore interface -- the seam between server.py and whichever backend
(SQLite or Postgres) actually holds the append-only journal.

Every method here is either an INSERT or a SELECT. There is no update/delete
method on this interface by design -- corrections are NOTE events, appended
like anything else, never edits to an existing row.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class JournalStore(ABC):
    @abstractmethod
    def append(self, event: dict[str, Any]) -> str:
        """Insert one journal event; return its id. Never updates, never deletes."""

    @abstractmethod
    def list(self, *, symbol: Optional[str], event_type: Optional[str], limit: int) -> list[dict[str, Any]]:
        """Newest-first journal rows, optionally filtered by symbol/event_type."""

    @abstractmethod
    def resolved_pairs(self, *, symbol: Optional[str], since_ts: float) -> list[dict[str, Any]]:
        """ENTRY rows joined to their resolving EXIT via ref_id, ts_utc >= since_ts.

        Each row: side, entry_px, exit_px, qty, entry_ts, exit_ts. Epoch
        filtering happens here, in SQL, not after the fact in Python --
        so a caller can never accidentally forget to apply it.
        """

    @abstractmethod
    def event_counts(self, *, symbol: Optional[str], since_ts: float) -> dict[str, int]:
        """Count of journal rows per event_type, ts_utc >= since_ts."""

    @abstractmethod
    def watchlist_add(self, *, symbol: str, exchange: str, screener: str) -> None:
        """Idempotent insert into the watchlist."""

    @abstractmethod
    def watchlist_list(self) -> list[dict[str, Any]]:
        """All watchlist rows, symbol order."""
