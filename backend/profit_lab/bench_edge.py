"""Compare always-on vs edge-filtered backtest for all strategies."""

from __future__ import annotations

import argparse
from pathlib import Path

from profit_lab.engine import run_lab_backtest
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
    args = parser.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    prices, _ohlcv, regimes = load_ohlcv(Path(args.data_dir), symbols)
    if args.max_bars:
        prices = prices.iloc[-args.max_bars:]
        regimes = regimes.iloc[-args.max_bars:]

    print("=" * 90)
    print(f"{'Strategy':<12s} {'Mode':<10s} {'Final':>10s} {'Return':>10s} {'Trades':>8s} {'Skipped':>10s} {'MaxDD':>8s}")
    print("=" * 90)
    for strat_name, strat in STRATEGIES.items():
        strat.__name__ = strat_name
        run_open = run_lab_backtest(
            prices,
            strat,
            regimes=regimes,
            fee_rate=0.001,
            slippage_rate=0.0005,
            margin_per_trade=args.margin_per_trade,
            leverage=args.leverage,
            funding_rate_8h=args.funding_rate_8h,
        )
        ret_open = (run_open.final_equity - run_open.initial_cash) / run_open.initial_cash
        run_edge = run_lab_backtest(
            prices,
            strat,
            regimes=regimes,
            fee_rate=0.001,
            slippage_rate=0.0005,
            margin_per_trade=args.margin_per_trade,
            leverage=args.leverage,
            funding_rate_8h=args.funding_rate_8h,
            edge_filter=True,
            min_edge_count=10,
            min_edge_value=0.0,
        )
        ret_edge = (run_edge.final_equity - run_edge.initial_cash) / run_edge.initial_cash

        peak = run_open.equity_curve[0]
        max_dd_open = 0.0
        for val in run_open.equity_curve:
            peak = max(peak, val)
            dd = (peak - val) / peak if peak > 0 else 0.0
            max_dd_open = max(max_dd_open, dd)
        peak = run_edge.equity_curve[0]
        max_dd_edge = 0.0
        for val in run_edge.equity_curve:
            peak = max(peak, val)
            dd = (peak - val) / peak if peak > 0 else 0.0
            max_dd_edge = max(max_dd_edge, dd)

        print(f"{strat_name:<12s} {'always-on':<10s} ${run_open.final_equity:>9,.0f} {ret_open:>9.2%} {len(run_open.trades):>8d} {'-':>10s} {max_dd_open:>7.2%}")
        print(f"{strat_name:<12s} {'edge-gate':<10s} ${run_edge.final_equity:>9,.0f} {ret_edge:>9.2%} {len(run_edge.trades):>8d} {run_edge.skipped_signals:>10d} {max_dd_edge:>7.2%}")
    print("=" * 90)


if __name__ == "__main__":
    main()
