"""Deterministic governed-backtest execution: no ReAct, no model in the loop.

A governed backtest is a transaction, not a conversation. This module builds
config.json + a fixed equal-weight buy-and-hold code/signal_engine.py
directly from structured parameters, invokes the existing backtest runner
the same way the agent's ``backtest`` tool does, and only reports success
when run_card.json exists with every required truth field. The model is not
consulted about what tool to call next or whether the run succeeded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from backtest.deterministic_strategies import BUY_AND_HOLD_SOURCE, BUY_AND_HOLD_TEMPLATE
from src.core.run_card_gate import REQUIRED_RUN_CARD_FIELDS, run_card_is_valid
from src.core.state import RunStateStore
from src.tools.backtest_tool import run_backtest

RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"


class GovernedBacktestError(ValueError):
    """Raised for invalid governed-backtest request parameters."""


def _validate_symbols(symbols: list[str]) -> list[str]:
    cleaned = [str(s).strip().upper() for s in symbols if str(s).strip()]
    if not cleaned:
        raise GovernedBacktestError("At least one symbol is required")
    non_usdt = [s for s in cleaned if not s.endswith("USDT") and not s.endswith("-USDT")]
    if non_usdt:
        raise GovernedBacktestError(
            f"Governed crypto backtests require explicit USDT pairs; got: {non_usdt}"
        )
    return cleaned


def run_governed_backtest(
    symbols: list[str],
    start_date: str,
    end_date: str,
    *,
    interval: str = "1D",
    initial_cash: float = 100_000.0,
    source: str = "binance",
    benchmark: bool = False,
) -> dict[str, Any]:
    """Execute a governed crypto backtest deterministically.

    ``benchmark=True`` opts into a BTC-USDT comparison resolved through the
    same Binance-first coverage queue and window-integrity law the strategy
    symbols use (see backtest.benchmark); it is off by default. There is no
    Alpha Vantage/CoinMarketCap/Yahoo path for a crypto benchmark -- if
    Binance and the exchange-native fallbacks can't cover the requested
    window, the run card reports benchmark_status accordingly instead of
    fabricating a comparison.

    Returns:
        Result dict with ``status`` ("success" | "failed"), ``run_id``,
        ``run_dir``, and either ``run_card`` (the required truth fields
        only) or ``reason``.
    """
    codes = _validate_symbols(symbols)

    state_store = RunStateStore()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = state_store.create_run_dir(RUNS_DIR)

    config = {
        "codes": codes,
        "start_date": start_date,
        "end_date": end_date,
        "interval": interval,
        "source": source,
        "engine": "daily",
        "initial_cash": initial_cash,
        "strategy_type": BUY_AND_HOLD_TEMPLATE,
    }
    if benchmark:
        config["benchmark"] = "BTC-USDT"
    state_store.save_request(
        run_dir,
        prompt="[governed backtest API] deterministic route, no ReAct",
        context={"codes": codes, "start_date": start_date, "end_date": end_date, "source": source},
    )
    (run_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    from write_receipt import receipted_write, mark_deterministic_baseline  # agent/write_receipt.py, top-level module

    signal_path = run_dir / "code" / "signal_engine.py"
    receipted_write(signal_path, BUY_AND_HOLD_SOURCE)
    mark_deterministic_baseline(
        run_dir,
        template=BUY_AND_HOLD_TEMPLATE,
        generated_by="deterministic_governed_endpoint",
    )

    runner_result_raw = run_backtest(str(run_dir))  # invokes backtest.runner exactly as the agent's tool does

    if not run_card_is_valid(run_dir):
        reason = "governed_backtest_missing_run_card"
        state_store.mark_failure(run_dir, reason)
        try:
            runner_result = json.loads(runner_result_raw)
        except (json.JSONDecodeError, TypeError):
            runner_result = {"raw": runner_result_raw}
        return {
            "status": "failed",
            "reason": reason,
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "runner_result": runner_result,
        }

    state_store.mark_success(run_dir)
    card = json.loads((run_dir / "run_card.json").read_text(encoding="utf-8"))
    result: dict[str, Any] = {
        "status": "success",
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "run_card": {field: card.get(field) for field in REQUIRED_RUN_CARD_FIELDS},
    }
    result["run_card"]["strategy_type"] = BUY_AND_HOLD_TEMPLATE
    result["run_card"]["signal_engine_sha256"] = card.get("signal_engine_sha256")
    if benchmark:
        metrics = card.get("metrics") or {}
        result["benchmark"] = {
            "status": metrics.get("benchmark_status"),
            "ticker": metrics.get("benchmark_ticker"),
            "fetch_source": metrics.get("benchmark_fetch_source"),
            "coverage_ratio": metrics.get("benchmark_coverage_ratio"),
            "coverage_ok": metrics.get("benchmark_coverage_ok"),
            "return": metrics.get("benchmark_return"),
            "error": metrics.get("benchmark_error"),
        }
    return result
