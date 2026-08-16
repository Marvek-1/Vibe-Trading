"""System and utility HTTP routes.

Mounted by ``agent/api_server.py`` via ``register_system_routes(app, ...)``.
"""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

# Paper-trading data lives under agent/paper_sessions -- resolved locally
# rather than imported from src.api.paper_session_routes, per this file's
# no-shared-modules rule. The path itself (parents[2] from this file) mirrors
# PAPER_SESSIONS_DIR there; if that ever moves, both need updating together.
_PAPER_SESSIONS_DIR = Path(__file__).resolve().parents[2] / "paper_sessions"
_STALE_MARK_THRESHOLD_SECONDS = 600  # 5x the 60s poll cadence -- margin for provider hiccups


def _latest_mark_age_seconds(session_dir: Path) -> Optional[float]:
    """Cheaply read just the last line of marks.jsonl without parsing the whole file.

    A health check that reparsed thousands of marks per session (some of
    these sessions have 3900+) to answer "is this fresh" would be slower
    than the thing it's checking. Reads a tail chunk instead of the full
    file; falls back to a full read only if a mark line is unusually long.
    """
    marks_path = session_dir / "marks.jsonl"
    if not marks_path.exists():
        return None
    try:
        with marks_path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 8192)
            f.seek(-chunk, os.SEEK_END)
            tail = f.read().decode("utf-8", errors="ignore")
        lines = [line for line in tail.splitlines() if line.strip()]
        if not lines:
            return None
        last = json.loads(lines[-1])
        ts = datetime.fromisoformat(last["timestamp"])
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


def _stale_paper_sessions() -> list[dict[str, Any]]:
    """Active (non-archived) paper sessions whose latest mark is older than
    the staleness threshold -- e.g. the loop died, or the API is reading a
    frozen snapshot instead of the live files (see the Docker bind-mount
    fix for agent/paper_sessions; this check exists so the next time that
    class of bug recurs, it's an alert instead of a four-day-later discovery).
    """
    if not _PAPER_SESSIONS_DIR.exists():
        return []
    stale = []
    for d in _PAPER_SESSIONS_DIR.iterdir():
        if not d.is_dir() or d.name.startswith("_") or not (d / "session.json").exists():
            continue
        age = _latest_mark_age_seconds(d)
        if age is not None and age > _STALE_MARK_THRESHOLD_SECONDS:
            stale.append({"session_id": d.name, "latest_mark_age_seconds": round(age)})
    return stale


# ---------------------------------------------------------------------------
# Pydantic models (defined locally -- NO shared modules, per maintainer rule)
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    """Health check payload."""
    status: str = Field(..., description="Service status: healthy or degraded")
    service: str = Field(..., description="Service name")
    timestamp: str = Field(..., description="Server timestamp")
    stale_paper_sessions: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Active paper sessions whose latest mark exceeds the staleness "
        "threshold, if any -- present only when status is degraded.",
    )


# ---------------------------------------------------------------------------
# Process termination
# ---------------------------------------------------------------------------


def _terminate_current_process() -> None:
    """Stop the current API process after the response has been sent."""
    time.sleep(0.25)
    os.kill(os.getpid(), signal.SIGTERM)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_system_routes(
    app: FastAPI,
    app_version: str | None = None,
) -> None:
    """Mount the system routes onto ``app``.

    Resolves ``_security``, ``_require_shutdown_authorization``, and
    ``APP_VERSION`` from the host ``api_server`` module via ``sys.modules``
    when not passed explicitly.
    """
    # Resolve host dependencies via sys.modules fallback
    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")

    if host is None:
        raise RuntimeError(
            "register_system_routes: api_server module not in sys.modules; "
            "ensure api_server is imported before calling this function"
        )

    _security = host._security
    _require_shutdown_authorization = host._require_shutdown_authorization
    _app_version = app_version if app_version is not None else host.APP_VERSION

    def _get_terminate_process():
        """Late-access _terminate_current_process for test monkeypatch compat."""
        h = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if h is not None:
            fn = getattr(h, "_terminate_current_process", None)
            if fn is not None:
                return fn
        return _terminate_current_process

    # --- Routes ---

    @app.get("/health", response_model=HealthResponse)
    async def health_check():
        """Liveness probe.

        Also checks paper-session data freshness: a process that's up but
        serving marks nobody's written in the last 10 minutes (a dead PM2
        loop, or the API reading a frozen snapshot instead of the live
        files) is not actually healthy, even though the HTTP layer is fine.
        Still returns 200 either way -- a container restart doesn't fix
        stale data, so this stays a liveness probe that also reports
        degraded, not a readiness gate that flaps the container.
        """
        stale = _stale_paper_sessions()
        return HealthResponse(
            status="degraded" if stale else "healthy",
            service="Vibe-Trading API",
            timestamp=datetime.now().isoformat(),
            stale_paper_sessions=stale or None,
        )

    @app.get("/correlation")
    async def get_correlation_matrix(
        codes: str = Query(..., description="Comma-separated asset codes, e.g. BTC-USDT,ETH-USDT,SPY"),
        days: int = Query(90, description="Lookback window in days", ge=7, le=365),
        method: str = Query("pearson", description="Correlation method: pearson or spearman"),
    ):
        """Compute cross-asset correlation matrix from daily returns.

        Fetches price data for each code via available data loaders,
        computes pairwise correlation of daily returns over the lookback window.
        """
        from backtest.correlation import compute_correlation_matrix

        code_list = [c.strip() for c in codes.split(",") if c.strip()]
        if len(code_list) < 2:
            raise HTTPException(status_code=400, detail="At least 2 asset codes required")
        if len(code_list) > 20:
            raise HTTPException(status_code=400, detail="Maximum 20 assets per request")
        if method not in ("pearson", "spearman"):
            raise HTTPException(status_code=400, detail="method must be 'pearson' or 'spearman'")

        try:
            result = compute_correlation_matrix(codes=code_list, days=days, method=method)
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Correlation computation failed: {exc}")

    @app.post("/system/shutdown")
    async def shutdown_local_api(
        background_tasks: BackgroundTasks,
        request: Request,
        cred: Optional[HTTPAuthorizationCredentials] = Security(_security),
    ):
        """Shut down the local API server after explicit local authorization."""
        _require_shutdown_authorization(request=request, cred=cred)
        client_host = request.client.host if request.client else ""
        if client_host not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Local access only")

        background_tasks.add_task(_get_terminate_process())
        return {
            "status": "shutting-down",
            "service": "Vibe-Trading API",
            "timestamp": datetime.now().isoformat(),
        }

    @app.get("/skills")
    async def list_skills():
        """List registered skills (name and description)."""
        from src.agent.skills import SkillsLoader

        loader = SkillsLoader()
        return [
            {
                "name": s.name,
                "description": s.description,
            }
            for s in loader.skills
        ]

    @app.get("/api")
    async def api_info():
        """Service metadata."""
        return {
            "service": "Vibe-Trading API",
            "version": _app_version,
            "docs": "/docs",
            "health": "/health",
        }
