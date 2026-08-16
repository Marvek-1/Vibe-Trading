"""CLI for the Mo Profit Lab.

Usage:
    python -m profit_lab.run momentum --symbols BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT,ADA-USDT,DOGE-USDT,LINK-USDT --data-dir agent/research/data
    python -m profit_lab.run trend --lookback-fast 12 --lookback-slow 26
    python -m profit_lab.run breakout --lookback 20
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, List

import pandas as pd

from profit_lab.analytics import report
from profit_lab.engine import run_lab_backtest
from profit_lab.models import Signal
from profit_lab.regime import build_regime_series
from profit_lab.strategies import momentum_continuation, trend_following, breakout, mean_reversion


def load_ohlcv(data_dir: Path, symbols: List[str]) -> tuple[pd.DataFrame, Dict[str, pd.DataFrame], pd.DataFrame]:
    """Load OHLCV data, return close matrix + full OHLCV dict + regime matrix."""
    frames: Dict[str, pd.DataFrame] = {}
    close_frames = {}
    for sym in symbols:
        path = data_dir / f"{sym}_1h.csv"
        if not path.exists():
            raise FileNotFoundError(f"missing data: {path}")
        df = pd.read_csv(path)
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("ts").sort_index()
        df = df[~df.index.duplicated(keep="first")]
        df = df.rename(columns={"open": "open", "high": "high", "low": "low", "close": "close"})
        frames[sym] = df
        close_frames[sym] = df["close"].rename(sym)
    close = pd.concat(close_frames.values(), axis=1).ffill().dropna()
    regimes = build_regime_series(frames)
    return close, frames, regimes


def _momentum_strategy_factory(args) -> Callable[[pd.DataFrame], List[Signal]]:
    return lambda prices: momentum_continuation(prices, args.lookback, args.top_k)


def _trend_strategy_factory(args) -> Callable[[pd.DataFrame], List[Signal]]:
    return lambda prices: trend_following(prices, args.lookback_fast, args.lookback_slow, args.top_k)


def _breakout_strategy_factory(args) -> Callable[[pd.DataFrame], List[Signal]]:
    return lambda prices: breakout(prices, args.lookback, args.top_k)


def _mean_rev_strategy_factory(args) -> Callable[[pd.DataFrame], List[Signal]]:
    return lambda prices: mean_reversion(prices, args.lookback, args.entry_z, args.exit_z)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Profit Lab strategy benchmark")
    parser.add_argument("strategy", choices=["momentum", "trend", "breakout", "mean_rev"])
    parser.add_argument("--symbols", default="BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT,ADA-USDT,DOGE-USDT,LINK-USDT")
    parser.add_argument("--data-dir", type=Path, default=Path("research/data"))
    parser.add_argument("--initial-cash", type=float, default=10_000.0)
    parser.add_argument("--margin-per-trade", type=float, default=100.0)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument("--slippage-rate", type=float, default=0.0005)
    parser.add_argument("--funding-rate-8h", type=float, default=0.0)
    parser.add_argument("--take-profit", type=float, default=None)
    parser.add_argument("--stop-loss", type=float, default=None)
    parser.add_argument("--trailing-stop", type=float, default=None)
    parser.add_argument("--max-hold-bars", type=int, default=None)
    parser.add_argument("--lookback", type=int, default=12)
    parser.add_argument("--lookback-fast", type=int, default=12)
    parser.add_argument("--lookback-slow", type=int, default=26)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--entry-z", type=float, default=2.0)
    parser.add_argument("--exit-z", type=float, default=0.5)
    parser.add_argument("--max-bars", type=int, default=None)
    parser.add_argument("--edge-filter", action="store_true")
    parser.add_argument("--min-edge-count", type=int, default=10)
    parser.add_argument("--min-edge-value", type=float, default=0.0)
    args = parser.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    prices, _ohlcv, regimes = load_ohlcv(args.data_dir, symbols)
    if args.max_bars:
        prices = prices.iloc[-args.max_bars:]
        regimes = regimes.iloc[-args.max_bars:]

    factory = {
        "momentum": _momentum_strategy_factory,
        "trend": _trend_strategy_factory,
        "breakout": _breakout_strategy_factory,
        "mean_rev": _mean_rev_strategy_factory,
    }[args.strategy]
    strategy = factory(args)
    strategy.__name__ = args.strategy

    run = run_lab_backtest(
        prices,
        strategy,
        regimes=regimes,
        initial_cash=args.initial_cash,
        margin_per_trade=args.margin_per_trade,
        leverage=args.leverage,
        fee_rate=args.fee_rate,
        slippage_rate=args.slippage_rate,
        funding_rate_8h=args.funding_rate_8h,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        trailing_stop=args.trailing_stop,
        max_hold_bars=args.max_hold_bars,
        edge_filter=args.edge_filter,
        min_edge_count=args.min_edge_count,
        min_edge_value=args.min_edge_value,
    )
    print(report(run))
    print(f"  skipped_signals        : {run.skipped_signals}")


if __name__ == "__main__":
    main()
