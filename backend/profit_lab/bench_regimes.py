"""Generate the Strategy × Regime expectancy matrix.

Usage:
    python -m profit_lab.bench_regimes --max-bars 2000
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Dict

import pandas as pd

from profit_lab.analytics import by_reason_and_regime, summary
from profit_lab.engine import run_lab_backtest
from profit_lab.models import Signal
from profit_lab.run import load_ohlcv
from profit_lab.strategies import momentum_continuation, trend_following, breakout, mean_reversion


STRATEGIES = {
    "momentum": lambda p: momentum_continuation(p, 12, 3),
    "trend": lambda p: trend_following(p, 12, 26, 3),
    "breakout": lambda p: breakout(p, 12, 3),
    "mean_rev": lambda p: mean_reversion(p, 20, 2.0, 0.5),
}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT,ADA-USDT,DOGE-USDT,LINK-USDT")
    parser.add_argument("--data-dir", default="research/data")
    parser.add_argument("--max-bars", type=int, default=None)
    parser.add_argument("--margin-per-trade", type=float, default=100.0)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--funding-rate-8h", type=float, default=0.0)
    parser.add_argument("--metric", default="expectancy", choices=["expectancy", "profit_factor", "net_pnl", "win_rate"])
    args = parser.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    prices, _ohlcv, regimes = load_ohlcv(Path(args.data_dir), symbols)
    if args.max_bars:
        prices = prices.iloc[-args.max_bars:]
        regimes = regimes.iloc[-args.max_bars:]

    # Aggregate matrix: {regime: {strategy: value}}
    matrix: Dict[str, Dict[str, float]] = defaultdict(dict)
    regime_sets: set = set()

    for strat_name, strat in STRATEGIES.items():
        print(f"Running {strat_name}...")
        strat.__name__ = strat_name
        run = run_lab_backtest(
            prices,
            strat,
            regimes=regimes,
            fee_rate=0.001,
            slippage_rate=0.0005,
            margin_per_trade=args.margin_per_trade,
            leverage=args.leverage,
            funding_rate_8h=args.funding_rate_8h,
        )
        print(f"  final equity: ${run.final_equity:,.2f}  trades: {len(run.trades)}")
        data = by_reason_and_regime(run.trades)
        for reason, regmap in data.items():
            for reg, stats in regmap.items():
                regime_sets.add(reg)
                matrix[reg][strat_name] = stats[args.metric]

    all_regimes = sorted(regime_sets)
    all_strats = list(STRATEGIES.keys())

    print("\n" + "=" * 80)
    print(f"STRATEGY × REGIME MATRIX  ({args.metric})")
    print("=" * 80)
    header = f"{'Regime':<26s}" + "".join(f"{s:>14s}" for s in all_strats)
    print(header)
    print("-" * 80)
    for reg in all_regimes:
        row = f"{reg:<26s}"
        for s in all_strats:
            val = matrix[reg].get(s, float("nan"))
            if args.metric in ("expectancy", "net_pnl"):
                row += f"{val:>14.2f}"
            else:
                row += f"{val:>14.4f}"
        print(row)

    # Best strategy per regime
    print("\n" + "=" * 80)
    print("BEST STRATEGY PER REGIME (by " + args.metric + ")")
    print("=" * 80)
    for reg in all_regimes:
        best = max(
            ((s, matrix[reg].get(s, float("-inf"))) for s in all_strats),
            key=lambda x: x[1],
        )
        print(f"  {reg:<26s}: {best[0]:<10s} ({best[1]:.4f})")


if __name__ == "__main__":
    main()
