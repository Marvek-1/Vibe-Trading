#!/usr/bin/env python3
"""Writes the frozen 8-symbol canary universe with a content hash.

Deliberately NOT the 32 currently-profitable many_bots_10x symbols -- those
were selected by looking at positive unrealized P&L on live marks, which is
look-ahead/winner-selection bias, not evidence of edge. This list is a fixed,
liquid, mechanically-chosen starting set for deployment-safety purposes only;
it is not a profitability claim.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

UNIVERSE = [
    "BTC-USDT",
    "ETH-USDT",
    "BNB-USDT",
    "SOL-USDT",
    "XRP-USDT",
    "ADA-USDT",
    "DOGE-USDT",
    "AVAX-USDT",
]


def canonical_bytes(symbols: list[str]) -> bytes:
    return json.dumps(symbols, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def universe_hash(symbols: list[str]) -> str:
    return hashlib.sha256(canonical_bytes(symbols)).hexdigest()


def write(path: str | Path) -> dict[str, object]:
    payload = {
        "universe_id": "canary8_v1",
        "symbols": UNIVERSE,
        "symbols_sha256": universe_hash(UNIVERSE),
        "note": (
            "Frozen mechanical starting set for the 5x/10x isolated-margin "
            "canary. Not derived from current unrealized P&L; do not rotate "
            "symbols based on live marks."
        ),
    }
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    result = write(Path(__file__).resolve().parent.parent / "paper_sessions" / "universe_frozen_canary8.json")
    print(json.dumps(result, indent=2))
