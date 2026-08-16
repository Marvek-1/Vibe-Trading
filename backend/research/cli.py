"""CLI for downloading Binance history and running the walk-forward gauntlet.

Examples:
    python -m research.cli download --symbols BTC-USDT,ETH-USDT --timeframe 1h --days 180
    python -m research.cli gauntlet --symbols BTC-USDT,ETH-USDT --timeframe 1h --fee-rate 0.001
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research import data_store, strategies, walkforward
from research.zoo_pipeline import run_zoo_pipeline, write_pipeline_artifacts


SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT", "ADA-USDT", "DOGE-USDT", "LINK-USDT"]


def cmd_download(args: argparse.Namespace) -> None:
    if args.symbols_file:
        text = Path(args.symbols_file).read_text()
        raw = text.replace("\n", ",").replace(" ", "").split(",")
        symbols = [s.strip().upper() for s in raw if s.strip()]
    else:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    until_ms = int(args.until * 1000) if args.until else None
    since_ms = int(time.time() * 1000) - int(args.days * 24 * 3600 * 1000)
    exchange = data_store._get_exchange(market_type=args.market_type)
    for symbol in symbols:
        print(f"downloading {symbol} {args.timeframe} from {datetime.fromtimestamp(since_ms/1000, tz=timezone.utc).isoformat()}")
        data_store.download_history(symbol, args.timeframe, since_ms, until_ms=until_ms, exchange=exchange, market_type=args.market_type)
    print("done")


def _parse_candidate_params(spec: str, base: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Override per-candidate backtest params from a CLI string.

    Format: 'name:k=v,k=v;name2:k=v'
    """
    out: dict[str, dict[str, Any]] = {k: dict(v) for k, v in base.items()}
    for segment in spec.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        if ":" not in segment:
            raise ValueError(f"invalid --candidate-params segment: {segment!r}")
        name, kv = segment.split(":", 1)
        name = name.strip()
        params = out.setdefault(name, {})
        for pair in kv.split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            params[k] = _coerce_value(v)
    return out


def _coerce_value(v: str) -> Any:
    v = v.strip()
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v


def cmd_download_universe(args: argparse.Namespace) -> None:
    """Download 1h OHLCV for the top-N liquid Binance USDT perpetuals."""
    if args.exchange.lower() != "binance":
        print(f"unsupported exchange: {args.exchange}; only binance is supported")
        return

    import ccxt

    exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 15_000})
    try:
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
    finally:
        exchange.close()

    perp = []
    for symbol, ticker in tickers.items():
        market = exchange.markets.get(symbol)
        if market is None:
            continue
        if not (market.get("quote") == "USDT" and market.get("type") == "swap" and market.get("active")):
            continue
        base = market["base"]
        perp.append((f"{base}-USDT", float(ticker.get("quoteVolume", 0.0) or 0.0)))
    perp.sort(key=lambda x: x[1], reverse=True)
    selected = [s[0] for s in perp[: args.top]]

    since_ms = int(time.time() * 1000) - int(args.days * 24 * 3600 * 1000)
    until_ms = int(time.time() * 1000)
    for symbol in selected:
        print(f"downloading {symbol} {args.timeframe} ...")
        data_store.download_history(symbol, args.timeframe, since_ms, until_ms=until_ms, market_type="swap")

    # Keep only symbols with complete, aligned history for the requested period
    if args.min_bars or args.min_coverage:
        requested_span_ms = int(args.days * 24 * 3600 * 1000)
        keep = []
        for s in selected:
            df = data_store.load_bars(s, args.timeframe)
            if len(df) < 2:
                continue
            if args.min_bars and len(df) < args.min_bars:
                continue
            span = int(df["ts"].max()) - int(df["ts"].min())
            if span < requested_span_ms * 0.95:
                continue
            # bar step is the most common timestamp difference
            step = int(df["ts"].diff().mode().iloc[0])
            expected = max(1, span // step + 1)
            if len(df) / expected < args.min_coverage:
                continue
            keep.append(s)
        selected = keep
        print(f"{len(selected)} symbols meet coverage requirement ({args.min_coverage}) and requested span")

    output = Path(args.output)
    output.write_text(",".join(selected) + "\n")
    print(f"top {len(selected)} USDT perpetuals by 24h quote volume -> {output.resolve()}")
    print("done")


def cmd_gauntlet(args: argparse.Namespace) -> None:
    if args.symbols_file:
        text = Path(args.symbols_file).read_text()
        raw = text.replace("\n", ",").replace(" ", "").split(",")
        symbols = [s.strip().upper() for s in raw if s.strip()]
    else:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    prices = data_store.load_close_matrix(symbols, args.timeframe)

    if args.candidates:
        candidate_names = [s.strip() for s in args.candidates.split(",") if s.strip()]
        unknown = set(candidate_names) - set(strategies.CANDIDATES)
        if unknown:
            raise ValueError(f"unknown candidates: {unknown}")
        candidates = {k: strategies.CANDIDATES[k] for k in candidate_names}
    else:
        candidates = strategies.CANDIDATES

    candidate_params = (
        _parse_candidate_params(args.candidate_params, strategies.CANDIDATE_PARAMS)
        if args.candidate_params
        else {k: dict(v) for k, v in strategies.CANDIDATE_PARAMS.items()}
    )
    result = walkforward.run_gauntlet(
        prices,
        candidates,
        strategies.BASELINES,
        fee_rate=args.fee_rate,
        slippage_rate=args.slippage_rate,
        initial_cash=args.cash,
        n_splits=args.n_splits,
        rebalance_every=args.rebalance_every,
        bars_per_year=args.bars_per_year,
        candidate_params=candidate_params,
    )
    print(json.dumps(result, indent=2, default=str))


def cmd_margin_compare(args: argparse.Namespace) -> None:
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    result = walkforward.run_margin_comparison(
        symbols,
        args.timeframe,
        args.fee_rate,
        args.slippage_rate,
        args.n_splits,
        args.rebalance_every,
        args.bars_per_year,
    )
    print(json.dumps(result, indent=2, default=str))


def cmd_per_symbol_isolated(args: argparse.Namespace) -> None:
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    result = walkforward.run_per_symbol_isolated(
        symbols,
        args.timeframe,
        args.fee_rate,
        args.slippage_rate,
        args.take_profit,
        args.stop_loss,
        cash_per_symbol=args.cash_per_symbol,
        n_splits=args.n_splits,
        rebalance_every=args.rebalance_every,
        bars_per_year=args.bars_per_year,
        save_to_db=args.save_to_db,
    )
    print(json.dumps(result, indent=2, default=str))


def cmd_zoo_gauntlet(args: argparse.Namespace) -> None:
    result = run_zoo_pipeline(
        universe=args.universe,
        period=args.period,
        zoo=args.zoo,
        bench_top=args.top,
        portfolio_top_n=args.top_n,
        fee_rate=args.fee_rate,
        slippage_rate=args.slippage_rate,
        initial_cash=args.cash,
        n_splits=args.n_splits,
        rebalance_every=args.rebalance_every,
        bars_per_year=args.bars_per_year,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        promotion_metric=args.promotion_metric,
        min_profit_factor=args.promote_min_profit_factor,
        min_test_splits=args.promote_min_test_splits,
        min_trades=args.promote_min_trades,
        max_drawdown=args.promote_max_drawdown,
        benchmark_name=args.benchmark,
        bench_output_dir=args.output_dir,
    )
    receipts = write_pipeline_artifacts(result, args.output, args.paper_output)
    qualified = [
        row["alpha_id"]
        for row in result["readiness"]
        if row["qualified_for_shadow_paper"]
    ]
    print(json.dumps({"receipts": receipts, "shadow_paper_candidates": qualified}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    dl = sub.add_parser("download")
    dl.add_argument("--symbols", default=",".join(SYMBOLS))
    dl.add_argument("--symbols-file", default=None, help="file with comma/newline separated symbols")
    dl.add_argument("--timeframe", default="1h")
    dl.add_argument("--days", type=float, default=180)
    dl.add_argument("--until", type=float, default=None, help="epoch seconds upper bound; omit for now")
    dl.add_argument("--market-type", default="spot", choices=["spot", "swap"], help="market type for symbol formatting")
    dl.set_defaults(func=cmd_download)

    du = sub.add_parser("download-universe")
    du.add_argument("--top", type=int, default=50)
    du.add_argument("--timeframe", default="1h")
    du.add_argument("--days", type=float, default=365)
    du.add_argument("--exchange", default="binance")
    du.add_argument("--output", default="universe_50.txt")
    du.add_argument("--min-bars", type=int, default=0, help="drop symbols with fewer recorded bars")
    du.add_argument("--min-coverage", type=float, default=0.0, help="minimum fraction of expected bars that must be present")
    du.set_defaults(func=cmd_download_universe)

    g = sub.add_parser("gauntlet")
    g.add_argument("--symbols", default=",".join(SYMBOLS))
    g.add_argument("--symbols-file", default=None)
    g.add_argument("--timeframe", default="1h")
    g.add_argument("--fee-rate", type=float, default=0.001)
    g.add_argument("--slippage-rate", type=float, default=0.0005)
    g.add_argument("--cash", type=float, default=10_000.0)
    g.add_argument("--n-splits", type=int, default=3)
    g.add_argument("--rebalance-every", type=int, default=1)
    g.add_argument("--bars-per-year", type=float, default=365.0 * 24)
    g.add_argument(
        "--candidates",
        default=None,
        help="comma-separated subset of CANDIDATES to test, e.g. top_gainers_spot",
    )
    g.add_argument(
        "--candidate-params",
        default=None,
        help="e.g. top_gainers_spot:take_profit=0.05,stop_loss=0.03",
    )
    g.set_defaults(func=cmd_gauntlet)

    p_margin = sub.add_parser(
        "margin-compare",
        help="Compare cross-margin vs isolated-margin on walk-forward splits.",
    )
    p_margin.add_argument("--symbols", default=",".join(SYMBOLS))
    p_margin.add_argument("--timeframe", default="1h")
    p_margin.add_argument("--fee-rate", type=float, default=0.001)
    p_margin.add_argument("--slippage-rate", type=float, default=0.0005)
    p_margin.add_argument("--n-splits", type=int, default=3)
    p_margin.add_argument("--rebalance-every", type=int, default=1, help="rebalance interval in bars (cross arm)")
    p_margin.add_argument("--bars-per-year", type=float, default=365.0 * 24)
    p_margin.set_defaults(func=cmd_margin_compare)

    p_iso = sub.add_parser(
        "per-symbol-isolated",
        help="Run $500 isolated buy-and-hold per symbol with TP/SL across walk-forward splits.",
    )
    p_iso.add_argument("--symbols", default=",".join(SYMBOLS))
    p_iso.add_argument("--timeframe", default="1h")
    p_iso.add_argument("--fee-rate", type=float, default=0.001)
    p_iso.add_argument("--slippage-rate", type=float, default=0.0005)
    p_iso.add_argument("--take-profit", type=float, required=True, help="take-profit fraction, e.g. 0.10 for 10%%")
    p_iso.add_argument("--stop-loss", type=float, required=True, help="stop-loss fraction, e.g. 0.05 for 5%%")
    p_iso.add_argument("--cash-per-symbol", type=float, default=500.0)
    p_iso.add_argument("--n-splits", type=int, default=3)
    p_iso.add_argument("--rebalance-every", type=int, default=1)
    p_iso.add_argument("--bars-per-year", type=float, default=365.0 * 24)
    p_iso.add_argument("--save-to-db", action="store_true",
                       help="Write each symbol's backtest session to the paper store so the dashboard shows them.")
    p_iso.set_defaults(func=cmd_per_symbol_isolated)

    g_zoo = sub.add_parser(
        "zoo-gauntlet",
        help="Bench alpha zoo on a universe, then run a TP/SL walk-forward gauntlet on the top-N by IR.",
    )
    g_zoo.add_argument("--universe", default="binance_perps_34")
    g_zoo.add_argument("--period", required=True)
    g_zoo.add_argument("--zoo", default=None, help="limit bench to one zoo, e.g. alpha101")
    g_zoo.add_argument("--top", type=int, default=20, help="how many alphas to promote from the IC bench")
    g_zoo.add_argument("--top-n", type=int, default=20, help="portfolio size per alpha strategy")
    g_zoo.add_argument("--fee-rate", type=float, default=0.001)
    g_zoo.add_argument("--slippage-rate", type=float, default=0.0005)
    g_zoo.add_argument("--cash", type=float, default=10_000.0)
    g_zoo.add_argument("--n-splits", type=int, default=10)
    g_zoo.add_argument("--rebalance-every", type=int, default=1)
    g_zoo.add_argument("--bars-per-year", type=float, default=365.0 * 24)
    g_zoo.add_argument("--take-profit", type=float, default=0.05)
    g_zoo.add_argument("--stop-loss", type=float, default=0.03)
    g_zoo.add_argument("--promotion-metric", default="sharpe")
    g_zoo.add_argument("--promote-min-profit-factor", type=float, default=1.2)
    g_zoo.add_argument("--promote-min-test-splits", type=int, default=2)
    g_zoo.add_argument("--promote-min-trades", type=int, default=30)
    g_zoo.add_argument("--promote-max-drawdown", type=float, default=0.25)
    g_zoo.add_argument("--benchmark", default="equal_weight")
    g_zoo.add_argument("--output", default="zoo_gauntlet.json")
    g_zoo.add_argument("--paper-output", default=None)
    g_zoo.add_argument(
        "--output-dir",
        default=None,
        help="alpha bench HTML report directory; default ~/.vibe-trading/reports",
    )
    g_zoo.set_defaults(func=cmd_zoo_gauntlet)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
