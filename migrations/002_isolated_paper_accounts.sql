-- Six-account paper/live trading isolation. Apply to the local PostgreSQL
-- instance before enabling VIBE_PAPER_DATABASE_URL in workers.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS paper_trading;

CREATE TABLE IF NOT EXISTS paper_trading.trading_accounts (
    account_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    timeframe TEXT NOT NULL CHECK (timeframe IN ('5m', '10m', '15m')),
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    leverage INTEGER NOT NULL CHECK (leverage IN (5, 10)),
    initial_capital NUMERIC(28, 10) NOT NULL CHECK (initial_capital > 0),
    cash_balance NUMERIC(28, 10) NOT NULL,
    realized_pnl NUMERIC(28, 10) NOT NULL DEFAULT 0,
    funding_pnl NUMERIC(28, 10) NOT NULL DEFAULT 0,
    fees NUMERIC(28, 10) NOT NULL DEFAULT 0 CHECK (fees >= 0),
    ledger_status TEXT NOT NULL DEFAULT 'OK',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (worker_id, mode),
    UNIQUE (strategy_id, timeframe, mode)
);

CREATE TABLE IF NOT EXISTS paper_trading.orders (
    order_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES paper_trading.trading_accounts(account_id),
    strategy_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    exchange_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    leverage INTEGER NOT NULL CHECK (leverage IN (5, 10)),
    status TEXT NOT NULL,
    quantity NUMERIC(36, 18) NOT NULL CHECK (quantity > 0),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (account_id, exchange_order_id)
);

CREATE TABLE IF NOT EXISTS paper_trading.fills (
    fill_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES paper_trading.trading_accounts(account_id),
    strategy_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    exchange_fill_id TEXT NOT NULL,
    exchange_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity NUMERIC(36, 18) NOT NULL CHECK (quantity > 0),
    price NUMERIC(36, 18) NOT NULL CHECK (price > 0),
    fee NUMERIC(28, 10) NOT NULL DEFAULT 0 CHECK (fee >= 0),
    filled_at TIMESTAMPTZ NOT NULL,
    UNIQUE (account_id, exchange_fill_id)
);

CREATE TABLE IF NOT EXISTS paper_trading.positions (
    account_id UUID NOT NULL REFERENCES paper_trading.trading_accounts(account_id),
    strategy_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity NUMERIC(36, 18) NOT NULL,
    average_entry_price NUMERIC(36, 18) NOT NULL,
    margin_used NUMERIC(28, 10) NOT NULL DEFAULT 0 CHECK (margin_used >= 0),
    unrealized_pnl NUMERIC(28, 10) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (account_id, symbol)
);

CREATE TABLE IF NOT EXISTS paper_trading.funding_events (
    funding_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES paper_trading.trading_accounts(account_id),
    strategy_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    funding_timestamp TIMESTAMPTZ NOT NULL,
    funding_rate NUMERIC(20, 16) NOT NULL,
    funding_pnl NUMERIC(28, 10) NOT NULL,
    UNIQUE (account_id, symbol, funding_timestamp)
);

CREATE TABLE IF NOT EXISTS paper_trading.equity_history (
    account_id UUID NOT NULL REFERENCES paper_trading.trading_accounts(account_id),
    strategy_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    marked_at TIMESTAMPTZ NOT NULL,
    cash_balance NUMERIC(28, 10) NOT NULL,
    margin_used NUMERIC(28, 10) NOT NULL,
    realized_pnl NUMERIC(28, 10) NOT NULL,
    unrealized_pnl NUMERIC(28, 10) NOT NULL,
    funding_pnl NUMERIC(28, 10) NOT NULL,
    fees NUMERIC(28, 10) NOT NULL,
    equity NUMERIC(28, 10) NOT NULL,
    PRIMARY KEY (account_id, marked_at),
    CHECK (abs(equity - (cash_balance + margin_used + unrealized_pnl)) < 0.000001)
);

CREATE TABLE IF NOT EXISTS paper_trading.worker_heartbeats (
    account_id UUID PRIMARY KEY REFERENCES paper_trading.trading_accounts(account_id),
    strategy_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    process_id INTEGER NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    last_trade_at TIMESTAMPTZ,
    risk_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (worker_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_account_created
    ON paper_trading.orders(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_fills_account_filled
    ON paper_trading.fills(account_id, filled_at);
CREATE INDEX IF NOT EXISTS idx_equity_account_marked
    ON paper_trading.equity_history(account_id, marked_at);

-- Live remains locked at the database boundary unless deliberately enabled by
-- a separate deployment migration. This release only provisions paper rows.
ALTER TABLE paper_trading.trading_accounts
    DROP CONSTRAINT IF EXISTS live_mode_release_lock;
ALTER TABLE paper_trading.trading_accounts
    ADD CONSTRAINT live_mode_release_lock CHECK (mode = 'paper') NOT VALID;
