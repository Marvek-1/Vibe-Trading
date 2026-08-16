"""Deterministic governed-backtest HTTP route.

Mounted by ``agent/api_server.py`` via ``register_governed_backtest_routes(app)``.

A governed backtest is a transaction, not a conversation: this endpoint does
not go through the session/ReAct agent loop at all. It builds config.json and
a fixed equal-weight buy-and-hold code/signal_engine.py directly from the
request body, executes the existing backtest runner exactly as the agent's
``backtest`` tool would, and reports success only when run_card.json exists
with every required truth field (see ``src.core.run_card_gate``). The
response body is built from run_card.json only -- there is no model-authored
narration in this path.

Crypto data source defaults to Binance (the queue member proven to deliver
full requested windows in prior coverage testing). Benchmark comparison
(``benchmark=true``) is opt-in and resolves BTC-USDT through the same
Binance-first, coverage-gated path -- see ``backtest.benchmark``.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

AuthDep = Callable[..., Any]


class GovernedBacktestRequest(BaseModel):
    """Structured, non-NL request for a deterministic governed crypto backtest."""

    symbols: list[str] = Field(..., description="Explicit USDT pairs, e.g. ['BTC-USDT', 'ETH-USDT']")
    start_date: str = Field(..., description="Backtest window start, e.g. '2024-01-01'")
    end_date: str = Field(..., description="Backtest window end, e.g. '2024-03-31'")
    interval: str = Field("1D", description="Bar interval")
    initial_cash: float = Field(100_000.0, description="Starting portfolio cash")
    source: str = Field("binance", description="Crypto data source; Binance-first by design")
    benchmark: bool = Field(False, description="Opt into a BTC-USDT benchmark comparison, Binance-first, coverage-gated")


def register_governed_backtest_routes(
    app: FastAPI,
    require_auth: Optional[AuthDep] = None,
) -> None:
    """Mount the governed backtest route onto ``app``."""
    if require_auth is None:
        import sys as _sys

        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if host is None:  # pragma: no cover
            raise RuntimeError(
                "register_governed_backtest_routes: api_server module not in "
                "sys.modules; pass require_auth explicitly"
            )
        require_auth = host.require_auth

    @app.post("/backtest/governed", dependencies=[Depends(require_auth)])
    async def run_governed_backtest_route(req: GovernedBacktestRequest):
        """Execute a governed crypto backtest deterministically (no agent loop)."""
        from src.backtest_governed import GovernedBacktestError, run_governed_backtest

        try:
            result = run_governed_backtest(
                symbols=req.symbols,
                start_date=req.start_date,
                end_date=req.end_date,
                interval=req.interval,
                initial_cash=req.initial_cash,
                source=req.source,
                benchmark=req.benchmark,
            )
        except GovernedBacktestError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return result
