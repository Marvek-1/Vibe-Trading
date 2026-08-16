import unittest

from unity import Candle, FundingVenue, MarketSnapshot, PortfolioState, Side, UnityStrategy


def candles(prices):
    return tuple(Candle(i, p * 0.999, p * 1.002, p * 0.998, p, 1000 + i) for i, p in enumerate(prices))


def market(prices, **kwargs):
    p = prices[-1]
    return MarketSnapshot("BTC-USDT", candles(prices), p * 0.99995, p * 1.00005, 100, 90, **kwargs)


def portfolio(**kwargs):
    return PortfolioState(10_000, 10_000, 10_000, **kwargs)


class UnityTests(unittest.TestCase):
    def test_risk_circuit_breaker_overrides_signal(self):
        p = PortfolioState(9_700, 10_000, 10_000)
        self.assertEqual(UnityStrategy().decide(market([100 + i for i in range(70)]), p).reason, "daily_loss_limit")

    def test_funding_arbitrage_is_delta_neutral_and_cost_aware(self):
        venues = (FundingVenue("cheap", 100.0, -0.0002, 8, 0.0001), FundingVenue("rich", 100.0, 0.0010, 8, 0.0001))
        action = UnityStrategy().decide(market([100 + (i % 2) * .01 for i in range(70)], funding=venues), portfolio())
        self.assertEqual(action.lane, "funding")
        self.assertEqual([o.side for o in action.orders], [Side.LONG, Side.SHORT])
        self.assertEqual(action.orders[0].notional, action.orders[1].notional)

    def test_directional_breakout_has_bounded_risk_and_target(self):
        prices = [100 + i * 0.05 for i in range(69)] + [110]
        action = UnityStrategy().decide(market(prices), portfolio())
        self.assertEqual(action.lane, "directional")
        self.assertIs(action.orders[0].side, Side.LONG)
        self.assertLess(action.stop_price, prices[-1])
        self.assertGreater(action.take_profit_price, prices[-1])
        self.assertLessEqual(action.orders[0].notional, 20_000)

    def test_choppy_market_quotes_both_sides_post_only(self):
        prices = [100 + (1 if i % 2 else -1) * 0.1 for i in range(70)]
        action = UnityStrategy().decide(market(prices), portfolio())
        self.assertEqual(action.lane, "quoting")
        self.assertEqual(len(action.orders), 2)
        self.assertTrue(all(o.order_type == "post_only" for o in action.orders))
        self.assertLess(action.orders[0].price, action.orders[1].price)

    def test_wide_spread_vetoes_every_lane(self):
        m = market([100 + i * .01 for i in range(70)])
        m = MarketSnapshot(m.symbol, m.candles, 99.0, 101.0, 100, 100)
        self.assertEqual(UnityStrategy().decide(m, portfolio()).reason, "spread_too_wide")


if __name__ == "__main__":
    unittest.main()
