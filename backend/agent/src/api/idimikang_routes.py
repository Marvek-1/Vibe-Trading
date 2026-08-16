"""Observer-only IdimIkang data-source routes.

These endpoints make IdimIkang a Vibe Trading research data source. They ingest
signals and observations into a local SQLite mirror. They do not expose any
execution permission and do not interact with IdimIkang's ``auto_executor.py``.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

import requests
from fastapi import Body, Depends, FastAPI, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from src.idimikang.stats import compute_signal_stats
from src.idimikang.store import get_store

AuthDep = Callable[..., Any]

IDIMIKANG_API_URL = os.environ.get("IDIMIKANG_API_URL", "http://127.0.0.1:41050/api").rstrip("/")
_TIMEOUT_S = float(os.environ.get("IDIMIKANG_SYNC_TIMEOUT_SECONDS", "5"))


def _events_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [event for event in payload if isinstance(event, dict)]
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("events"), list):
        return [event for event in payload["events"] if isinstance(event, dict)]
    if isinstance(payload.get("event"), dict):
        return [payload["event"]]
    return [payload]


def _fetch_idimikang_signals(limit: int) -> list[dict[str, Any]]:
    response = requests.get(f"{IDIMIKANG_API_URL}/signals", timeout=_TIMEOUT_S)
    response.raise_for_status()
    body = response.json()
    signals = body.get("signals", body if isinstance(body, list) else [])
    if not isinstance(signals, list):
        raise ValueError("IdimIkang /signals response did not contain a signal list")
    return [s for s in signals[:limit] if isinstance(s, dict)]


def register_idimikang_routes(
    app: FastAPI,
    require_auth: Optional[AuthDep] = None,
) -> None:
    """Mount IdimIkang data-source routes onto ``app``."""
    if require_auth is None:
        import sys as _sys

        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if host is None:  # pragma: no cover
            raise RuntimeError(
                "register_idimikang_routes: api_server module not in "
                "sys.modules; pass require_auth explicitly"
            )
        require_auth = host.require_auth

    @app.get("/data-sources/idimikang/status", dependencies=[Depends(require_auth)])
    async def idimikang_status():
        store = get_store()
        return {
            "source": "idimikang",
            "observer_only": True,
            "execution_allowed": False,
            "ingestion_api": IDIMIKANG_API_URL,
            "store": store.stats(),
        }

    @app.post("/data-sources/idimikang/ingest", dependencies=[Depends(require_auth)])
    async def ingest_idimikang_events(payload: Any = Body(...)):
        events = _events_from_payload(payload)
        if not events:
            raise HTTPException(status_code=422, detail="provide event or events")

        store = get_store()
        try:
            results = [store.ingest(event) for event in events]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "source": "idimikang",
            "observer_only": True,
            "execution_allowed": False,
            "received": len(events),
            "inserted": sum(1 for result in results if result["inserted"]),
            "duplicates": sum(1 for result in results if not result["inserted"]),
            "events": [result["event"] for result in results],
        }

    @app.post("/data-sources/idimikang/sync", dependencies=[Depends(require_auth)])
    async def sync_idimikang_signals(limit: int = Query(200, ge=1, le=1000)):
        try:
            signals = await run_in_threadpool(_fetch_idimikang_signals, limit)
        except requests.exceptions.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"IdimIkang API unavailable: {exc}") from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"IdimIkang sync failed: {exc}") from exc

        store = get_store()
        try:
            results = [store.ingest(signal) for signal in signals]
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=f"IdimIkang payload invalid: {exc}") from exc
        return {
            "source": "idimikang",
            "observer_only": True,
            "execution_allowed": False,
            "fetched": len(signals),
            "inserted": sum(1 for result in results if result["inserted"]),
            "duplicates": sum(1 for result in results if not result["inserted"]),
        }

    @app.get("/data-sources/idimikang/events", dependencies=[Depends(require_auth)])
    async def list_idimikang_events(
        symbol: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=1000),
        include_tainted: bool = Query(True),
    ):
        return {
            "source": "idimikang",
            "observer_only": True,
            "execution_allowed": False,
            "events": get_store().list_events(
                symbol=symbol,
                limit=limit,
                include_tainted=include_tainted,
            ),
        }

    @app.get("/data-sources/idimikang/signal-stats", dependencies=[Depends(require_auth)])
    async def idimikang_signal_stats(
        limit: int = Query(1000, ge=1, le=10000),
        include_tainted: bool = Query(True),
    ):
        """Return honest profit-factor and win/loss metrics for all emitted signals."""
        raw_signals = get_store().get_all_signals(limit=limit, include_tainted=include_tainted)
        try:
            metrics = compute_signal_stats(raw_signals)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "source": "idimikang",
            "observer_only": True,
            "execution_allowed": False,
            "metrics": metrics,
        }
