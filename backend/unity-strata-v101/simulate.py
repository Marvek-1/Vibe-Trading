from __future__ import annotations

import argparse
import csv
import gzip
import json
from dataclasses import dataclass

from unity import Candle, MarketSnapshot, PortfolioState, Side, UnityStrategy


@dataclass
class Position:
    side: Side
    notional: float
    entry: float
    stop: float
    target: float
    entry_bar: int


def load_binance_trades(paths: list[str], minutes: int = 5) -> list[Candle]:
    buckets: dict[int, list[tuple[float, float]]] = {}
    width = minutes * 60_000
    for path in paths:
      with gzip.open(path, "rt") as stream:
       for line in stream:
            try:
                payload = json.loads(line.split(" ", 1)[1])["data"]
            except (IndexError, KeyError, json.JSONDecodeError):
                continue
            if payload.get("e") != "trade":
                continue
            bucket = int(payload["T"]) // width * width
            buckets.setdefault(bucket, []).append((float(payload["p"]), float(payload["q"])))
    out = []
    for timestamp, trades in sorted(buckets.items()):
        prices = [x[0] for x in trades]
        out.append(Candle(timestamp, prices[0], max(prices), min(prices), prices[-1], sum(x[1] for x in trades)))
    return out


def run(candles: list[Candle], initial: float, fee: float, slippage: float) -> tuple[dict, list[dict]]:
    if len(candles) < 63:
        raise ValueError(f"simulation requires at least 63 closed candles; received {len(candles)}")
    engine = UnityStrategy()
    equity = peak = initial
    position = None
    losses = 0
    fees = 0.0
    trades = []
    curve = [equity]
    for i in range(61, len(candles) - 1):
        bar, next_bar = candles[i], candles[i + 1]
        if position:
            exit_price = reason = None
            if position.side is Side.LONG:
                if next_bar.low <= position.stop: exit_price, reason = position.stop * (1 - slippage), "stop"
                elif next_bar.high >= position.target: exit_price, reason = position.target * (1 - slippage), "target"
            else:
                if next_bar.high >= position.stop: exit_price, reason = position.stop * (1 + slippage), "stop"
                elif next_bar.low <= position.target: exit_price, reason = position.target * (1 + slippage), "target"
            if exit_price is None and i - position.entry_bar >= 24:
                exit_price, reason = next_bar.open * (1 - slippage if position.side is Side.LONG else 1 + slippage), "time"
            if exit_price is not None:
                direction = 1 if position.side is Side.LONG else -1
                gross = position.notional * direction * (exit_price / position.entry - 1)
                exit_fee = position.notional * fee
                fees += exit_fee
                net = gross - exit_fee
                equity += net
                losses = losses + 1 if net < 0 else 0
                trades.append({"entry_bar": position.entry_bar, "exit_bar": i + 1, "side": position.side.value, "entry": position.entry, "exit": exit_price, "net_pnl": net, "reason": reason})
                position = None
        if position is None:
            snap = MarketSnapshot("BTCUSDT", tuple(candles[:i + 1]), bar.close * (1 - .000005), bar.close * (1 + .000005), 1, 1)
            state = PortfolioState(equity, initial, peak, consecutive_losses=losses)
            action = engine.decide(snap, state)
            if action.lane == "directional" and action.orders:
                intent = action.orders[0]
                entry = next_bar.open * (1 + slippage if intent.side is Side.LONG else 1 - slippage)
                entry_fee = intent.notional * fee
                fees += entry_fee
                equity -= entry_fee
                position = Position(intent.side, intent.notional, entry, action.stop_price, action.take_profit_price, i + 1)
        peak = max(peak, equity)
        curve.append(equity)
    if position:
        last = candles[-1].close
        direction = 1 if position.side is Side.LONG else -1
        gross = position.notional * direction * (last / position.entry - 1)
        exit_fee = position.notional * fee
        fees += exit_fee
        net = gross - exit_fee
        equity += net
        trades.append({"entry_bar": position.entry_bar, "exit_bar": len(candles) - 1, "side": position.side.value, "entry": position.entry, "exit": last, "net_pnl": net, "reason": "end"})
    wins = sum(t["net_pnl"] > 0 for t in trades)
    max_dd = max((max(curve[:i + 1]) - v) / max(curve[:i + 1]) for i, v in enumerate(curve)) if curve else 0
    buy_hold = initial * (candles[-1].close / candles[61].close - 1)
    summary = {"bars": len(candles), "initial_equity": initial, "ending_equity": equity, "net_pnl": equity - initial, "return_pct": (equity / initial - 1) * 100, "buy_hold_pnl": buy_hold, "fees": fees, "trades": len(trades), "win_rate_pct": wins / len(trades) * 100 if trades else 0, "max_drawdown_pct": max_dd * 100}
    return summary, trades


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("data", nargs="+")
    parser.add_argument("--initial", type=float, default=100.0)
    parser.add_argument("--fee", type=float, default=0.0005)
    parser.add_argument("--slippage", type=float, default=0.0002)
    parser.add_argument("--trades-csv", default="simulation_trades.csv")
    args = parser.parse_args()
    loaded = load_binance_trades(args.data)
    summary, trades = run(loaded, args.initial, args.fee, args.slippage)
    with open(args.trades_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["entry_bar", "exit_bar", "side", "entry", "exit", "net_pnl", "reason"])
        writer.writeheader(); writer.writerows(trades)
    print(json.dumps(summary, indent=2))
