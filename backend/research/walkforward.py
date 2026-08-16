"""Chronological train/validate/test splits and gauntlet runner.

No peeking: parameters are selected on the validation window, but all
reported numbers come from the held-out test window that follows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from research import data_store, strategies
from research.backtest import BacktestResult, Strategy, run_backtest


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
#  paper‑store helpers (imported lazily to avoid breakage if not installed)
# ---------------------------------------------------------------------------
def _persist_backtest_session(
    session_id: str,
    symbol: str,
    initial_cash: float,
    entry_time_iso: str,
    entry_price: float,
    exit_time_iso: str,
    exit_price: float,
    qty: float,
    fee_paid: float,
    final_equity: float,
    margin_mode: str = "isolated",
) -> None:
    """Create a synthetic paper session in the PostgreSQL store so the
    dashboard can display the per‑symbol backtest results."""
    try:
        from paper_store import get_store
    except ImportError:
        print(f"paper_store not available – skipping DB write for {session_id}")
        return

    store = get_store()

    # Session config dict – mirrors a minimal session.json
    session = {
        "strategy_type": "per_symbol_isolated_backtest",
        "symbols": [symbol],
        "initial_cash": initial_cash,
        "entry_time": entry_time_iso,
        "entry_prices": {symbol: entry_price},
        "fee_rate": 0.001,          # not critical for display
        "slippage_modeled": False,
        "fees_modeled": True,
        "cash_accounting_note": "backtest",
        "accounting_schema_version": 2,
        "accounting_status": "OK",
        "margin_mode": margin_mode,
        "source": "backtest",
        "price_kind": "ohlcv_close",
        "min_rebalance_notional": 0.0,
    }
    store.upsert_session(session_id, session, session.get("cash_accounting_note"))

    # Entry trade
    entry_trade = {
        "timestamp": entry_time_iso,
        "symbol": symbol,
        "side": "BUY",
        "qty": qty,
        "price": entry_price,
        "notional": initial_cash,
        "fee_paid": fee_paid,
        "reason": "entry",
    }
    store.insert_trade(session_id, entry_trade)

    # Exit trade (mark as SELL to realise PnL)
    exit_trade = {
        "timestamp": exit_time_iso,
        "symbol": symbol,
        "side": "SELL",
        "qty": qty,
        "price": exit_price,
        "notional": qty * exit_price,
        "fee_paid": fee_paid,
        "reason": "exit",
    }
    store.insert_trade(session_id, exit_trade)

    # Final mark
    mark = {
        "timestamp": exit_time_iso,
        "prices": {symbol: exit_price},
        "position_values": {symbol: 0.0},   # after exit
        "cash_remaining": final_equity,
        "equity": final_equity,
        "pnl": final_equity - initial_cash,
        "pnl_pct": (final_equity - initial_cash) / initial_cash if initial_cash else 0.0,
    }
    store.insert_mark(session_id, mark)


# ---------------------------------------------------------------------------
#  Original split logic and gauntlet
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Split:
    name: str
    prices: pd.DataFrame
    train: pd.DataFrame | None
    validate: pd.DataFrame | None
    test: pd.DataFrame


def make_splits(
    prices: pd.DataFrame,
    n_splits: int = 3,
    train_frac: float = 0.5,
    validate_frac: float = 0.1,
    test_frac: float = 0.1,
) -> list[Split]:
    bars = len(prices)
    train_bars = max(2, int(bars * train_frac))
    out_bars = (bars - train_bars) // n_splits
    if out_bars < 2:
        raise ValueError("not enough bars for the requested number of splits")
    denom = validate_frac + test_frac
    validate_bars = max(1, int(out_bars * validate_frac / denom))
    test_bars = max(1, out_bars - validate_bars)
    splits: list[Split] = []
    cursor = train_bars
    for i in range(n_splits):
        validate_start = cursor
        validate_end = min(validate_start + validate_bars, bars)
        test_end = min(validate_end + test_bars, bars)
        if test_end <= validate_end:
            break
        splits.append(
            Split(
                name=f"split_{i+1}",
                prices=prices,
                train=prices.iloc[:validate_start],
                validate=prices.iloc[validate_start:validate_end],
                test=prices.iloc[validate_end:test_end],
            )
        )
        cursor = validate_end + test_bars
    return splits


def evaluate_strategy(
    split: Split,
    strategy: Strategy,
    initial_cash: float,
    fee_rate: float,
    slippage_rate: float,
    rebalance_every: int = 1,
    band_fraction: float = 0.0,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    window: str = "test",
) -> tuple[str, BacktestResult]:
    if window not in {"validate", "test"}:
        raise ValueError(f"unsupported evaluation window: {window}")
    prices = split.test if window == "test" else split.validate
    if prices is None:
        raise ValueError(f"split {split.name} has no {window} window")
    return split.name, run_backtest(
        prices,
        strategy,
        initial_cash=initial_cash,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
        rebalance_every=rebalance_every,
        band_fraction=band_fraction,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )


def rank_by_metric(results: dict[str, BacktestResult], metric: str = "sharpe") -> list[tuple[str, float]]:
    return sorted(
        ((name, result.metrics.get(metric, 0.0)) for name, result in results.items()),
        key=lambda x: x[1],
        reverse=True,
    )


def best_candidate(
    validate_results: dict[str, BacktestResult],
    metric: str = "sharpe",
) -> str:
    ranked = rank_by_metric(validate_results, metric)
    return ranked[0][0]


def run_gauntlet(
    prices: pd.DataFrame,
    candidates: dict[str, Strategy],
    baselines: dict[str, Strategy],
    fee_rate: float = 0.001,
    slippage_rate: float = 0.0005,
    initial_cash: float = 10_000.0,
    n_splits: int = 3,
    rebalance_every: int = 1,
    bars_per_year: float = 365.0 * 24,
    promotion_metric: str = "sharpe",
    candidate_params: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    candidate_params = candidate_params or {}
    splits = make_splits(prices, n_splits=n_splits)
    if not splits:
        raise ValueError("not enough bars for any walk-forward split")

    selected: dict[str, dict[str, float]] = {}
    test_results: dict[str, list[dict[str, Any]]] = {name: [] for name in list(candidates) + list(baselines)}

    for split in splits:
        if split.validate is None:
            continue
        validate_scores: dict[str, float] = {}
        for name, strategy in candidates.items():
            kwargs = candidate_params.get(name, {})
            _, result = evaluate_strategy(
                split,
                strategy,
                initial_cash,
                fee_rate,
                slippage_rate,
                rebalance_every,
                band_fraction=kwargs.get("band_fraction", 0.0),
                stop_loss=kwargs.get("stop_loss", None),
                take_profit=kwargs.get("take_profit", None),
                window="validate",
            )
            validate_scores[name] = result.metrics.get(promotion_metric, 0.0)
        if not validate_scores:
            continue
        chosen = max(validate_scores, key=validate_scores.get)
        selected[split.name] = {chosen: validate_scores[chosen]}

        chosen_kwargs = candidate_params.get(chosen, {})
        _, chosen_result = evaluate_strategy(
            split,
            candidates[chosen],
            initial_cash,
            fee_rate,
            slippage_rate,
            rebalance_every,
            band_fraction=chosen_kwargs.get("band_fraction", 0.0),
            stop_loss=chosen_kwargs.get("stop_loss", None),
            take_profit=chosen_kwargs.get("take_profit", None),
        )
        test_results[chosen].append({
            "split": split.name,
            **chosen_result.metrics,
            "trades": chosen_result.trades,
            "turnover": chosen_result.turnover,
            "fees_paid": chosen_result.fees_paid,
        })

        for name, strategy in baselines.items():
            _, result = evaluate_strategy(
                split,
                strategy,
                initial_cash,
                fee_rate,
                slippage_rate,
                rebalance_every,
            )
            test_results[name].append({
                "split": split.name,
                **result.metrics,
                "trades": result.trades,
                "turnover": result.turnover,
                "fees_paid": result.fees_paid,
            })

    def aggregate(records: list[dict[str, Any]]) -> dict[str, float]:
        if not records:
            return {}
        keys = [k for k in records[0] if k != "split"]
        return {k: float(sum(r[k] for r in records) / len(records)) for k in keys}

    summary = {name: aggregate(recs) for name, recs in test_results.items() if recs}
    per_split: dict[str, dict[str, dict[str, float]]] = {}
    for name, recs in test_results.items():
        for rec in recs:
            split_name = rec["split"]
            per_split.setdefault(split_name, {})[name] = {k: v for k, v in rec.items() if k != "split"}
    return {
        "splits": [s.name for s in splits],
        "selected_per_split": selected,
        "test_summary": summary,
        "per_split": per_split,
    }


def run_margin_comparison(
    symbols: list[str],
    timeframe: str,
    fee_rate: float,
    slippage_rate: float,
    n_splits: int = 3,
    rebalance_every: int = 1,
    bars_per_year: float = 365.0 * 24,
) -> dict:
    """
    Compare cross-margin (equal-weight rebalance) vs isolated-margin (buy-and-hold)
    on the same walk-forward splits used by the gauntlet.

    Returns per-fold metrics for both arms plus the delta.
    """
    prices = data_store.load_close_matrix(symbols, timeframe)
    if prices.empty:
        raise ValueError("No data loaded -- run `download` first")

    splits = make_splits(prices, n_splits=n_splits)
    if not splits:
        raise ValueError("Not enough data for splits")

    result = {
        "cross_metrics": [],
        "isolated_metrics": [],
        "per_fold": [],
    }

    for fold_idx, split in enumerate(splits):
        # Cross arm
        cross_result = run_backtest(
            split.test,
            strategies.equal_weight,
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
            rebalance_every=rebalance_every,
            bars_per_year=bars_per_year,
        )

        # Isolated arm
        isolated_result = run_backtest(
            split.test,
            strategies.buy_and_hold,
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
            rebalance_every=rebalance_every,
            bars_per_year=bars_per_year,
        )

        fold_result = {
            "fold": fold_idx + 1,
            "cross": cross_result.metrics,
            "isolated": isolated_result.metrics,
            "delta_total_return": isolated_result.metrics["total_return"] - cross_result.metrics["total_return"],
            "delta_max_drawdown": isolated_result.metrics["max_drawdown"] - cross_result.metrics["max_drawdown"],
            "delta_sharpe": isolated_result.metrics["sharpe"] - cross_result.metrics["sharpe"],
        }
        result["per_fold"].append(fold_result)
        result["cross_metrics"].append(cross_result.metrics)
        result["isolated_metrics"].append(isolated_result.metrics)

    avg_delta_return = sum(f["delta_total_return"] for f in result["per_fold"]) / len(result["per_fold"])
    avg_delta_dd = sum(f["delta_max_drawdown"] for f in result["per_fold"]) / len(result["per_fold"])
    result["summary"] = {
        "avg_cross_return": sum(m["total_return"] for m in result["cross_metrics"]) / len(result["cross_metrics"]),
        "avg_isolated_return": sum(m["total_return"] for m in result["isolated_metrics"]) / len(result["isolated_metrics"]),
        "avg_delta_return": avg_delta_return,
        "avg_delta_drawdown": avg_delta_dd,
        "interpretation": "isolated outperforms cross" if avg_delta_return > 0 else "cross outperforms isolated",
    }

    return result


def run_per_symbol_isolated(
    symbols: list[str],
    timeframe: str,
    fee_rate: float,
    slippage_rate: float,
    take_profit: float,
    stop_loss: float,
    cash_per_symbol: float = 500.0,
    n_splits: int = 3,
    rebalance_every: int = 1,
    bars_per_year: float = 365.0 * 24,
    save_to_db: bool = False,
) -> dict:
    """
    Run an isolated buy-and-hold arm for each symbol separately with a fixed
    allocation of `cash_per_symbol` and a shared TP/SL discipline.

    If ``save_to_db`` is True, every fold's result is persisted as a synthetic
    paper session in the database so the dashboard can display it.
    """
    prices = data_store.load_close_matrix(symbols, timeframe)
    if prices.empty:
        raise ValueError("No data loaded -- run `download` first")

    splits = make_splits(prices, n_splits=n_splits)
    if not splits:
        raise ValueError("Not enough data for splits")

    result: dict = {"splits": [s.name for s in splits], "symbols": symbols, "per_symbol": {}}

    cost_rate = fee_rate + slippage_rate

    for split in splits:
        for symbol in symbols:
            sym_prices = split.test[[symbol]]
            if len(sym_prices) < 2:
                continue
            res = run_backtest(
                sym_prices,
                strategies.buy_and_hold,
                initial_cash=cash_per_symbol,
                fee_rate=fee_rate,
                slippage_rate=slippage_rate,
                rebalance_every=rebalance_every,
                bars_per_year=bars_per_year,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            record = {
                "split": split.name,
                "final_equity": res.metrics["final_equity"],
                "total_return": res.metrics["total_return"],
                "max_drawdown": res.metrics["max_drawdown"],
                "trades": res.trades,
                "exited": res.trades > 1,
            }
            result["per_symbol"].setdefault(symbol, []).append(record)

            # Persist if requested
            if save_to_db:
                # Get entry and exit times/prices from the test data
                test_sym = split.test[[symbol]]
                entry_bar = test_sym.iloc[0]
                exit_bar = test_sym.iloc[-1]
                entry_price = float(entry_bar[symbol])
                exit_price = float(exit_bar[symbol])
                entry_time_iso = datetime.fromtimestamp(int(entry_bar.name) / 1000, tz=timezone.utc).isoformat()
                exit_time_iso = datetime.fromtimestamp(int(exit_bar.name) / 1000, tz=timezone.utc).isoformat()

                # Compute quantity and fees (approximate from entry trade)
                cost = cash_per_symbol * cost_rate
                investable = cash_per_symbol - cost
                qty = investable / entry_price
                fee_paid = cost

                session_id = f"bt_isol_{symbol}_{split.name}_{_now_iso().replace(':','-').replace('+','_')}"

                _persist_backtest_session(
                    session_id=session_id,
                    symbol=symbol,
                    initial_cash=cash_per_symbol,
                    entry_time_iso=entry_time_iso,
                    entry_price=entry_price,
                    exit_time_iso=exit_time_iso,
                    exit_price=exit_price,
                    qty=qty,
                    fee_paid=fee_paid,
                    final_equity=res.metrics["final_equity"],
                )

    for symbol, folds in result["per_symbol"].items():
        if not folds:
            continue
        result["per_symbol"][symbol].append({
            "split": "avg",
            "final_equity": sum(f["final_equity"] for f in folds) / len(folds),
            "total_return": sum(f["total_return"] for f in folds) / len(folds),
            "max_drawdown": sum(f["max_drawdown"] for f in folds) / len(folds),
            "trades": sum(f["trades"] for f in folds) / len(folds),
            "exited": None,
        })

    return result