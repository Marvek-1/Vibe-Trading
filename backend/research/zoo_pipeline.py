from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from research import strategies, walkforward
from research.alpha_adapter import alpha_to_strategy
from write_receipt import receipted_write

SCHEMA_VERSION = "1.0"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _selection_counts(result: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for selected in result.get("selected_per_split", {}).values():
        counts.update(selected.keys())
    return counts


def evaluate_readiness(
    result: dict[str, Any],
    candidate_names: list[str],
    *,
    min_profit_factor: float,
    min_test_splits: int,
    min_trades: int,
    max_drawdown: float,
    benchmark_name: str,
) -> list[dict[str, Any]]:
    summary = result.get("test_summary", {})
    benchmark = summary.get(benchmark_name, {})
    benchmark_return = benchmark.get("total_return")
    counts = _selection_counts(result)
    readiness: list[dict[str, Any]] = []
    for name in candidate_names:
        metrics = summary.get(name, {})
        tested_splits = counts.get(name, 0)
        total_return = metrics.get("total_return")
        profit_factor = metrics.get("profit_factor")
        drawdown = metrics.get("max_drawdown")
        average_closed_trades = metrics.get("closed_trades")
        total_closed_trades = int(round(float(average_closed_trades or 0) * tested_splits))
        return_edge = (
            float(total_return) - float(benchmark_return)
            if total_return is not None and benchmark_return is not None
            else None
        )
        gates = {
            "positive_test_return": total_return is not None and total_return > 0,
            "finite_profit_factor": profit_factor is not None and math.isfinite(float(profit_factor)),
            "minimum_profit_factor": profit_factor is not None and float(profit_factor) > min_profit_factor,
            "minimum_test_splits": tested_splits >= min_test_splits,
            "minimum_closed_trades": total_closed_trades >= min_trades,
            "maximum_drawdown": drawdown is not None and float(drawdown) > -abs(max_drawdown),
        }
        readiness.append(
            {
                "alpha_id": name,
                "qualified_for_shadow_paper": all(gates.values()),
                "gates": gates,
                "test_splits": tested_splits,
                "total_closed_trades": total_closed_trades,
                "return_edge_vs_benchmark": return_edge,
                "metrics": metrics,
            }
        )
    return readiness


def build_paper_handoff(
    readiness: list[dict[str, Any]],
    *,
    universe: str,
    period: str,
    symbols: list[str],
    top_n: int,
    fee_rate: float,
    slippage_rate: float,
    rebalance_every: int,
    take_profit: float | None,
    stop_loss: float | None,
) -> dict[str, Any]:
    candidates = []
    for row in readiness:
        if not row["qualified_for_shadow_paper"]:
            continue
        candidates.append(
            {
                "alpha_id": row["alpha_id"],
                "status": "eligible_for_shadow_paper",
                "auto_start": False,
                "strategy_type": "alpha_zoo_top_n",
                "universe": universe,
                "symbols": symbols,
                "period": period,
                "top_n": top_n,
                "rebalance_every_bars": rebalance_every,
                "fee_rate": fee_rate,
                "slippage_rate": slippage_rate,
                "take_profit": take_profit,
                "stop_loss": stop_loss,
                "research_metrics": row["metrics"],
                "readiness_gates": row["gates"],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "destination": "paper_trading_shadow_only",
        "production_authority": "none",
        "auto_start": False,
        "candidates": candidates,
    }


def run_zoo_pipeline(
    *,
    universe: str,
    period: str,
    zoo: str | None,
    bench_top: int,
    portfolio_top_n: int,
    fee_rate: float,
    slippage_rate: float,
    initial_cash: float,
    n_splits: int,
    rebalance_every: int,
    bars_per_year: float,
    take_profit: float | None,
    stop_loss: float | None,
    promotion_metric: str,
    min_profit_factor: float,
    min_test_splits: int,
    min_trades: int,
    max_drawdown: float,
    benchmark_name: str,
    bench_output_dir: str | None = None,
) -> dict[str, Any]:
    from src.tools import alpha_bench_tool

    if bench_top <= 0 or portfolio_top_n <= 0:
        raise ValueError("bench_top and portfolio_top_n must be greater than zero")
    if n_splits <= 0 or rebalance_every <= 0:
        raise ValueError("n_splits and rebalance_every must be greater than zero")
    if min_test_splits <= 0 or min_trades < 0:
        raise ValueError("readiness split and trade thresholds are invalid")
    if benchmark_name not in strategies.BASELINES:
        raise ValueError(f"unknown benchmark {benchmark_name!r}")

    bench = alpha_bench_tool.run_alpha_bench(
        universe=universe,
        period=period,
        zoo=zoo,
        top=bench_top,
        output_dir=bench_output_dir,
    )
    if bench.get("status") != "ok":
        raise RuntimeError(f"alpha bench failed: {bench.get('error', 'unknown error')}")
    top_rows = bench.get("top", [])
    if not top_rows:
        raise ValueError("alpha bench produced no candidates")

    panel = alpha_bench_tool._load_universe_panel(universe, period)
    prices = panel["close"]
    if not isinstance(prices, pd.DataFrame) or prices.empty:
        raise ValueError("universe loader produced no close matrix")

    candidates: dict[str, strategies.Strategy] = {}
    rejected_adapters: list[dict[str, str]] = []
    for row in top_rows:
        alpha_id = str(row["id"])
        try:
            candidates[alpha_id] = alpha_to_strategy(alpha_id, panel, top_n=portfolio_top_n)
        except (ValueError, KeyError, RuntimeError) as exc:
            rejected_adapters.append({"alpha_id": alpha_id, "reason": str(exc)})
    if not candidates:
        raise ValueError("none of the benched alphas could be adapted")

    candidate_params = {
        name: {"take_profit": take_profit, "stop_loss": stop_loss}
        for name in candidates
    }
    gauntlet = walkforward.run_gauntlet(
        prices,
        candidates,
        strategies.BASELINES,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
        initial_cash=initial_cash,
        n_splits=n_splits,
        rebalance_every=rebalance_every,
        bars_per_year=bars_per_year,
        candidate_params=candidate_params,
        promotion_metric=promotion_metric,
    )
    readiness = evaluate_readiness(
        gauntlet,
        list(candidates),
        min_profit_factor=min_profit_factor,
        min_test_splits=min_test_splits,
        min_trades=min_trades,
        max_drawdown=max_drawdown,
        benchmark_name=benchmark_name,
    )
    paper_handoff = build_paper_handoff(
        readiness,
        universe=universe,
        period=period,
        symbols=[str(symbol) for symbol in prices.columns],
        top_n=portfolio_top_n,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
        rebalance_every=rebalance_every,
        take_profit=take_profit,
        stop_loss=stop_loss,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "pipeline": "alpha_zoo_to_gauntlet_to_shadow_paper",
        "configuration": {
            "universe": universe,
            "period": period,
            "zoo": zoo,
            "bench_top": bench_top,
            "portfolio_top_n": portfolio_top_n,
            "fee_rate": fee_rate,
            "slippage_rate": slippage_rate,
            "initial_cash": initial_cash,
            "n_splits": n_splits,
            "rebalance_every": rebalance_every,
            "bars_per_year": bars_per_year,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "promotion_metric": promotion_metric,
        },
        "universe": {
            "symbols": [str(symbol) for symbol in prices.columns],
            "symbol_count": len(prices.columns),
            "bar_count": len(prices),
            "first_bar": str(prices.index[0]),
            "last_bar": str(prices.index[-1]),
        },
        "bench": bench,
        "adapter_rejections": rejected_adapters,
        "gauntlet": gauntlet,
        "readiness": readiness,
        "paper_handoff": paper_handoff,
    }


def write_pipeline_artifacts(
    result: dict[str, Any],
    output_path: str | Path,
    paper_output_path: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    output = Path(output_path).expanduser().resolve()
    paper_output = (
        Path(paper_output_path).expanduser().resolve()
        if paper_output_path is not None
        else output.with_name(f"{output.stem}.paper{output.suffix or '.json'}")
    )
    payload = json.dumps(_json_safe(result), indent=2, sort_keys=True, allow_nan=False) + "\n"
    handoff = json.dumps(_json_safe(result["paper_handoff"]), indent=2, sort_keys=True, allow_nan=False) + "\n"
    return {
        "research": receipted_write(output, payload),
        "paper_handoff": receipted_write(paper_output, handoff),
    }
