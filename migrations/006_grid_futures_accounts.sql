-- Grid Futures workers (bounded_grid_v1): grid_futures_5x / grid_futures_10x.
--
-- Two schema changes are required before these accounts can be inserted:
--
-- 1. trading_accounts_timeframe_check only allowed candle timeframes
--    ('5m','10m','15m'). A grid ladder isn't candle-driven -- it ticks every
--    5 seconds watching for a price level crossing -- so it needs a
--    timeframe token that says that honestly instead of lying about which
--    candle it evaluates on.
--
-- 2. trading_accounts_strategy_id_timeframe_mode_key assumed exactly one
--    worker per (strategy_id, timeframe, mode) -- true when "strategy_id"
--    meant "control" or "candidate" (one worker per arm per timeframe), but
--    grid_futures_5x and grid_futures_10x both run strategy_id
--    'bounded_grid_v1' at timeframe 'tick': same strategy, two leverage
--    variants. worker_id is already the real per-account identity
--    (uq_worker_account, uq_account_identity from migration 003); this
--    constraint was never load-bearing for that and only blocks a second,
--    legitimate leverage variant of the same strategy.
BEGIN;

ALTER TABLE paper_trading.trading_accounts
    DROP CONSTRAINT IF EXISTS trading_accounts_timeframe_check;
ALTER TABLE paper_trading.trading_accounts
    ADD CONSTRAINT trading_accounts_timeframe_check
    CHECK (timeframe IN ('5m', '10m', '15m', 'tick'));

ALTER TABLE paper_trading.trading_accounts
    DROP CONSTRAINT IF EXISTS trading_accounts_strategy_id_timeframe_mode_key;

INSERT INTO paper_trading.trading_accounts
    (account_id, strategy_id, worker_id, timeframe, mode, leverage,
     initial_capital, cash_balance, realized_pnl, funding_pnl, fees,
     margin_used, unrealized_pnl, current_equity, ledger_status)
VALUES
    ('b7e2b9d0-6b7a-4c4a-9c8b-1a2f3e4d5c6a', 'bounded_grid_v1', 'grid_futures_5x', 'tick', 'paper', 5,
     1000, 1000, 0, 0, 0, 0, 0, 1000, 'OK'),
    ('c8f3cae1-7c8b-4d5b-ad9c-2b3f4e5d6c7b', 'bounded_grid_v1', 'grid_futures_10x', 'tick', 'paper', 10,
     1000, 1000, 0, 0, 0, 0, 0, 1000, 'OK')
ON CONFLICT (worker_id, mode) DO NOTHING;

COMMIT;
