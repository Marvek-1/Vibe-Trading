from __future__ import annotations

import math
import unittest

from agent.paper_accounting_guard import (
    PriceSnapshotError,
    assess_accounting,
    normalize_price_snapshot,
    position_ledger_differences,
)


class PriceSnapshotTests(unittest.TestCase):
    def test_complete_snapshot_is_normalized(self) -> None:
        result = normalize_price_snapshot(
            ["BTC-USDT", "ETH-USDT"],
            {"BTC-USDT": "60000.5", "ETH-USDT": 3000, "EXTRA": 1},
        )
        self.assertEqual(
            result,
            {"BTC-USDT": 60000.5, "ETH-USDT": 3000.0},
        )

    def test_missing_price_fails_before_mutation(self) -> None:
        with self.assertRaises(PriceSnapshotError) as ctx:
            normalize_price_snapshot(
                ["BTC-USDT", "ETH-USDT"],
                {"BTC-USDT": 60000},
            )
        self.assertEqual(ctx.exception.missing_symbols, ("ETH-USDT",))

    def test_zero_nan_and_infinite_prices_are_rejected(self) -> None:
        with self.assertRaises(PriceSnapshotError) as ctx:
            normalize_price_snapshot(
                ["A", "B", "C"],
                {"A": 0, "B": math.nan, "C": math.inf},
            )
        self.assertEqual(ctx.exception.invalid_symbols, ("A", "B", "C"))


class LedgerTests(unittest.TestCase):
    def test_book_and_trade_quantities_match(self) -> None:
        self.assertEqual(
            position_ledger_differences(
                {"BTC-USDT": 0.1},
                {"BTC-USDT": {"open_qty": 0.1}},
            ),
            {},
        )

    def test_book_and_trade_quantity_mismatch_is_reported(self) -> None:
        differences = position_ledger_differences(
            {"BTC-USDT": 0.1},
            {"BTC-USDT": {"open_qty": 0.0}},
        )
        self.assertAlmostEqual(differences["BTC-USDT"]["difference"], 0.1)


class AccountingDecisionTests(unittest.TestCase):
    def test_complete_reconciled_ledger_is_ok(self) -> None:
        decision = assess_accounting(
            configured_symbols=["BTC-USDT"],
            initial_cash=10_000,
            equity=9_500,
            realized_pnl=-400,
            unrealized_pnl=-100,
        )
        self.assertEqual(decision.state, "OK")
        self.assertAlmostEqual(decision.residual or 0.0, 0.0)

    def test_missing_configured_mark_is_deferred_not_frozen(self) -> None:
        decision = assess_accounting(
            configured_symbols=["BTC-USDT", "ETH-USDT"],
            initial_cash=10_000,
            equity=8_000,
            realized_pnl=0,
            unrealized_pnl=None,
            stale_mark_symbols=["ETH-USDT"],
        )
        self.assertEqual(decision.state, "DEFERRED")
        self.assertEqual(decision.reason, "INCOMPLETE_PRICE_SNAPSHOT")

    def test_unconfigured_open_symbol_is_a_ledger_error(self) -> None:
        decision = assess_accounting(
            configured_symbols=["BTC-USDT"],
            initial_cash=10_000,
            equity=8_000,
            realized_pnl=0,
            unrealized_pnl=None,
            stale_mark_symbols=["REMOVED-USDT"],
        )
        self.assertEqual(decision.state, "ERROR")
        self.assertEqual(decision.reason, "LEDGER_SYMBOL_MISMATCH")

    def test_position_mismatch_is_error_even_when_equity_looks_valid(self) -> None:
        decision = assess_accounting(
            configured_symbols=["BTC-USDT"],
            initial_cash=10_000,
            equity=10_000,
            realized_pnl=0,
            unrealized_pnl=0,
            position_differences={
                "BTC-USDT": {
                    "book_qty": 0.0,
                    "trade_qty": 1.0,
                    "difference": -1.0,
                }
            },
        )
        self.assertEqual(decision.state, "ERROR")
        self.assertEqual(decision.reason, "POSITION_LEDGER_MISMATCH")

    def test_two_thousand_dollar_residual_is_error(self) -> None:
        decision = assess_accounting(
            configured_symbols=["BTC-USDT"],
            initial_cash=10_000,
            equity=7_776,
            realized_pnl=-224,
            unrealized_pnl=0,
        )
        self.assertEqual(decision.state, "ERROR")
        self.assertEqual(decision.reason, "SELF_FINANCING_RESIDUAL")
        self.assertAlmostEqual(decision.residual or 0.0, -2_000.0)


if __name__ == "__main__":
    unittest.main()
