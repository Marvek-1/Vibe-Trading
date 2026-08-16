"""IdimIkang signal feed and ingested Vibe data-source events, read-only."""

from __future__ import annotations

import json
import os
from typing import Any

import requests

from src.agent.tools import BaseTool

IDIMIKANG_API_URL = os.environ.get("IDIMIKANG_API_URL", "http://127.0.0.1:41050/api").rstrip("/")
_TIMEOUT_S = 5.0


class IdimikangSignalsTool(BaseTool):
    """Fetch IdimIkang signals and ingested Vibe data-source events."""

    name = "get_idimikang_signals"
    description = (
        "Fetch IdimIkang scanner context plus normalized events already ingested "
        "into Vibe Trading. Read-only; use for research/backtest context, never "
        "for order placement."
    )
    parameters = {
        "type": "object",
        "properties": {
            "include_live": {
                "type": "boolean",
                "description": "Fetch current live open signals from the IdimIkang API.",
                "default": True,
            },
            "include_stats": {
                "type": "boolean",
                "description": "Include the engine stats summary (profit factor, wins, losses).",
                "default": True,
            },
            "include_ingested": {
                "type": "boolean",
                "description": "Include normalized events ingested into Vibe Trading's local store.",
                "default": True,
            },
            "symbol": {
                "type": "string",
                "description": "Optional symbol filter for ingested events, e.g. BTCUSDT.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum ingested events to return.",
                "default": 50,
                "minimum": 1,
                "maximum": 500,
            },
        },
        "required": [],
    }

    def execute(self, **kwargs: Any) -> str:
        include_live = kwargs.get("include_live", True)
        include_stats = kwargs.get("include_stats", True)
        include_ingested = kwargs.get("include_ingested", True)
        symbol = kwargs.get("symbol")
        limit = int(kwargs.get("limit", 50) or 50)
        result: dict[str, Any] = {}

        if include_live:
            try:
                resp = requests.get(f"{IDIMIKANG_API_URL}/signals", timeout=_TIMEOUT_S)
                resp.raise_for_status()
                result["signals"] = resp.json().get("signals", [])
            except requests.exceptions.RequestException as exc:
                result["signals_error"] = str(exc)

        if include_stats:
            try:
                resp = requests.get(
                    f"{IDIMIKANG_API_URL}/stats", params={"all_history": "true"}, timeout=_TIMEOUT_S
                )
                resp.raise_for_status()
                result["stats"] = resp.json()
            except requests.exceptions.RequestException as exc:
                result["stats_error"] = str(exc)

        if include_ingested:
            try:
                from src.idimikang.store import get_store

                result["ingested_events"] = get_store().list_events(
                    symbol=str(symbol).upper() if symbol else None,
                    limit=max(1, min(limit, 500)),
                )
            except Exception as exc:  # noqa: BLE001 - tool should return structured errors
                result["ingested_events_error"] = str(exc)

        return json.dumps(result, default=str)
