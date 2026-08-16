BEGIN;

INSERT INTO paper_trading.trading_accounts
    (account_id, strategy_id, worker_id, timeframe, mode, leverage,
     initial_capital, cash_balance, realized_pnl, funding_pnl, fees,
     margin_used, unrealized_pnl, current_equity, ledger_status)
VALUES
    ('da4f02c8-b49e-4c87-a3bc-9399b78f60a1', 'funding_rate_zscore',
     'morning_glory', 'tick', 'paper', 5,
     1000, 1000, 0, 0, 0, 0, 0, 1000, 'OK')
ON CONFLICT (worker_id, mode) DO NOTHING;

COMMIT;
