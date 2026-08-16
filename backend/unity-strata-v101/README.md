# Unity Futures Strategy

Unity is a framework-neutral decision engine for crypto perpetual futures. It does not place orders or pretend fills happened. Your trading deck supplies closed candles, a current order book, funding snapshots, portfolio state, and execution adapters; Unity returns explicit order intents.

## What was synthesized

- **Jesse:** pure strategy lifecycle and explicit position/risk semantics.
- **Hummingbot:** cross-venue funding-rate normalization and delta-neutral paired legs.
- **hftbacktest:** order-book imbalance, volatility-aware spreads, inventory-skewed reservation price, and post-only quoting.
- **Freqtrade:** closed-candle processing, long/short symmetry, startup warmup, and strategy-level protections. No GPL source was copied.

The lanes are mutually exclusive. Funding arbitrage has priority when its estimated 24-hour carry remains positive after basis difference, two-way fees, and slippage. Otherwise a trend-efficiency gate chooses directional breakout or two-sided quoting. A single risk governor can veto all lanes.

## Run the verified tests

```bash
cd unity_strategy
python -m unittest discover -s tests -v
```

Run the included cost-bearing Binance futures trade-stream simulator with one or more continuous `.gz` captures:

```bash
python simulate.py day1.gz day2.gz --initial 100 --fee 0.0005 --slippage 0.0002
```

The simulator refuses fewer than 63 complete candles. Its default 5-minute aggregation therefore needs at least 5.25 hours of continuous trades; meaningful validation needs months across multiple regimes.

## Deck integration

```python
from unity import UnityStrategy

action = UnityStrategy().decide(market_snapshot, portfolio_state)
for intent in action.orders:
    execution_adapter.submit(intent)
```

Only pass fully closed candles. Populate `FundingVenue` with the exchange's current mark, funding interval, taker fee, and an executable-size slippage estimate. Treat paired funding orders atomically: if one leg fails, immediately cancel or hedge the other. Post-only intents must be cancelled when the next decision changes their price.

## Non-negotiable production gates

1. Map intent fields to the deck's actual exchange adapter and validate tick/lot rounding.
2. Backtest with the intended exchange's historical funding, mark prices, fees, and delisting universe.
3. Run walk-forward and untouched out-of-sample periods; reject parameter sets unstable across neighboring values.
4. Model latency, queue position, partial fills, liquidation tiers, and failed paired legs.
5. Paper trade until reconciliation proves orders, fills, funding, equity, and P&L agree with the exchange.
6. Begin live at minimum size with exchange-native reduce-only emergency stops.

This package is executable and testable. It is not a profit guarantee. No honest strategy can provide one.

## Provenance and licence

This is an original MIT-licensed implementation informed by the uploaded projects' public architecture and examples. hftbacktest and Jesse are MIT; Hummingbot is Apache-2.0; Freqtrade is GPL-3.0. Their code is not redistributed here.
