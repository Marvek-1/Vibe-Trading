"""Shared success gate for governed backtests: run_card.json or it didn't happen.

A model's (or a deterministic route's) narration is not evidence a backtest
produced governed output. This is the one mechanical check both the ReAct
agent loop and the deterministic governed-backtest route call before either
is allowed to report success.
"""

from __future__ import annotations

import json
from pathlib import Path

REQUIRED_RUN_CARD_FIELDS = (
    "provenance_valid",
    "data_source_provenance",
    "window_integrity",
    "statistically_evaluable",
    "hypothesis_supported",
)


def run_card_is_valid(run_dir: Path) -> bool:
    """Return True only if run_dir/run_card.json exists and carries every truth field.

    Presence of the required keys is what's checked, not their truthiness --
    e.g. hypothesis_supported is legitimately null until a real evidence gate
    (deflated Sharpe, benchmark comparison, etc.) exists.
    """
    card_path = Path(run_dir) / "run_card.json"
    if not card_path.exists():
        return False
    try:
        card = json.loads(card_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(card, dict):
        return False
    return all(field in card for field in REQUIRED_RUN_CARD_FIELDS)
