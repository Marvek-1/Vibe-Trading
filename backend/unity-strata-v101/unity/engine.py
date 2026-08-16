from __future__ import annotations

from dataclasses import dataclass

from .indicators import atr, efficiency_ratio, ema, realized_volatility, zscore
from .models import Action, FundingVenue, MarketSnapshot, OrderIntent, PortfolioState, Side


@dataclass(frozen=True)
class UnityConfig:
    risk_per_trade: float = 0.005
    max_gross_leverage: float = 2.0
    max_daily_loss: float = 0.02
    max_drawdown: float = 0.08
    max_consecutive_losses: int = 4
    trend_efficiency_min: float = 0.32
    funding_edge_min: float = 0.0015
    funding_horizon_hours: float = 24.0
    min_reward_risk: float = 1.8
    max_spread_bps: float = 12.0
    max_latency_ms: float = 750.0


class UnityStrategy:
    """Pure decision engine. The trading deck owns market data, orders, and fills."""

    def __init__(self, config: UnityConfig | None = None) -> None:
        self.config = config or UnityConfig()

    def decide(self, market: MarketSnapshot, portfolio: PortfolioState) -> Action:
        veto = self._risk_veto(market, portfolio)
        if veto:
            return Action(lane="flat", reason=veto)

        funding = self._funding_lane(market, portfolio)
        if funding.orders:
            return funding

        closes = [c.close for c in market.candles]
        if len(closes) < 61:
            return Action(lane="flat", reason="warmup_requires_61_closed_candles")

        er = efficiency_ratio(closes, 20)
        vol = realized_volatility(closes, 30)
        if er >= self.config.trend_efficiency_min:
            return self._directional_lane(market, portfolio, er, vol)
        return self._quoting_lane(market, portfolio, er, vol)

    def _risk_veto(self, market: MarketSnapshot, portfolio: PortfolioState) -> str | None:
        if portfolio.equity <= 0 or portfolio.daily_start_equity <= 0 or portfolio.peak_equity <= 0:
            return "invalid_equity_state"
        if (portfolio.daily_start_equity - portfolio.equity) / portfolio.daily_start_equity >= self.config.max_daily_loss:
            return "daily_loss_limit"
        if (portfolio.peak_equity - portfolio.equity) / portfolio.peak_equity >= self.config.max_drawdown:
            return "drawdown_limit"
        if portfolio.consecutive_losses >= self.config.max_consecutive_losses:
            return "loss_streak_circuit_breaker"
        if portfolio.open_notional / portfolio.equity >= self.config.max_gross_leverage:
            return "gross_leverage_limit"
        if market.bid <= 0 or market.ask <= market.bid:
            return "invalid_book"
        spread_bps = (market.ask - market.bid) / ((market.ask + market.bid) / 2) * 10_000
        if spread_bps > self.config.max_spread_bps:
            return "spread_too_wide"
        if market.latency_ms > self.config.max_latency_ms:
            return "market_data_stale"
        return None

    def _funding_lane(self, market: MarketSnapshot, portfolio: PortfolioState) -> Action:
        if len(market.funding) < 2:
            return Action(lane="funding")
        best: tuple[float, FundingVenue, FundingVenue] | None = None
        for long_venue in market.funding:
            for short_venue in market.funding:
                if long_venue.venue == short_venue.venue:
                    continue
                long_daily = long_venue.funding_rate * self.config.funding_horizon_hours / long_venue.interval_hours
                short_daily = short_venue.funding_rate * self.config.funding_horizon_hours / short_venue.interval_hours
                carry = short_daily - long_daily
                basis_cost = abs(long_venue.mark_price - short_venue.mark_price) / min(long_venue.mark_price, short_venue.mark_price)
                round_trip_cost = 2 * (long_venue.taker_fee + short_venue.taker_fee + long_venue.slippage + short_venue.slippage)
                edge = carry - basis_cost - round_trip_cost
                if best is None or edge > best[0]:
                    best = edge, long_venue, short_venue
        if best is None or best[0] < self.config.funding_edge_min:
            return Action(lane="funding", reason="funding_edge_below_cost_buffer", diagnostics={"best_edge": best[0] if best else 0.0})
        edge, long_venue, short_venue = best
        leg = min(portfolio.equity * 0.25, max(0.0, portfolio.equity * self.config.max_gross_leverage - portfolio.open_notional) / 2)
        if leg <= 0:
            return Action(lane="flat", reason="no_risk_capacity")
        return Action(
            lane="funding",
            orders=(OrderIntent(long_venue.venue, Side.LONG, leg, "market"), OrderIntent(short_venue.venue, Side.SHORT, leg, "market")),
            confidence=min(1.0, edge / (self.config.funding_edge_min * 3)),
            reason="delta_neutral_funding_edge_after_basis_fees_slippage",
            diagnostics={"net_24h_edge": edge, "long_venue": long_venue.venue, "short_venue": short_venue.venue},
        )

    def _directional_lane(self, market: MarketSnapshot, portfolio: PortfolioState, er: float, vol: float) -> Action:
        closes = [c.close for c in market.candles]
        fast, slow = ema(closes, 20), ema(closes, 50)
        price = closes[-1]
        prior_high = max(c.high for c in market.candles[-21:-1])
        prior_low = min(c.low for c in market.candles[-21:-1])
        side = Side.LONG if fast > slow and price > prior_high else Side.SHORT if fast < slow and price < prior_low else Side.FLAT
        if side is Side.FLAT:
            return Action(lane="directional", reason="trend_without_confirmed_breakout", diagnostics={"efficiency": er})
        risk_distance = max(atr(market.candles, 14) * 2.2, price * vol * 2.0)
        risk_budget = portfolio.equity * self.config.risk_per_trade
        notional = min(risk_budget / (risk_distance / price), portfolio.equity * self.config.max_gross_leverage - portfolio.open_notional)
        if notional <= 0:
            return Action(lane="flat", reason="no_risk_capacity")
        stop = price - risk_distance if side is Side.LONG else price + risk_distance
        target_distance = risk_distance * self.config.min_reward_risk
        target = price + target_distance if side is Side.LONG else price - target_distance
        return Action(
            lane="directional",
            orders=(OrderIntent(None, side, notional, "market"),),
            confidence=min(1.0, er), stop_price=stop, take_profit_price=target,
            reason="ema_trend_plus_20_bar_breakout",
            diagnostics={"efficiency": er, "realized_vol": vol, "atr": atr(market.candles, 14)},
        )

    def _quoting_lane(self, market: MarketSnapshot, portfolio: PortfolioState, er: float, vol: float) -> Action:
        if market.bid_depth <= 0 or market.ask_depth <= 0:
            return Action(lane="flat", reason="depth_required_for_quoting")
        mid = (market.bid + market.ask) / 2
        imbalance = (market.bid_depth - market.ask_depth) / (market.bid_depth + market.ask_depth)
        closes = [c.close for c in market.candles]
        reversion = -max(-2.5, min(2.5, zscore(closes, 30)))
        inventory_ratio = portfolio.position_notional / max(portfolio.equity, 1e-12)
        reservation = mid * (1 + 0.00015 * imbalance + 0.00008 * reversion - 0.00025 * inventory_ratio)
        half_spread = max((market.ask - market.bid) / 2, mid * (0.00015 + 2.5 * vol))
        capacity = max(0.0, portfolio.equity * self.config.max_gross_leverage - portfolio.open_notional)
        quote_notional = min(portfolio.equity * 0.02, capacity / 2)
        if quote_notional <= 0:
            return Action(lane="flat", reason="no_risk_capacity")
        bid_price, ask_price = min(market.bid, reservation - half_spread), max(market.ask, reservation + half_spread)
        return Action(
            lane="quoting",
            orders=(OrderIntent(None, Side.LONG, quote_notional, "post_only", bid_price), OrderIntent(None, Side.SHORT, quote_notional, "post_only", ask_price)),
            confidence=max(0.05, 1 - er),
            reason="inventory_skewed_microstructure_quotes",
            diagnostics={"book_imbalance": imbalance, "reservation_price": reservation, "realized_vol": vol},
        )
