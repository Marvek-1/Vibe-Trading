CREATE TABLE IF NOT EXISTS paper_trading.positions_v2 (
    account_id UUID NOT NULL REFERENCES paper_trading.trading_accounts(account_id),
    trade_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity NUMERIC(36,18) NOT NULL,
    average_entry_price NUMERIC(36,18) NOT NULL,
    margin_used NUMERIC(28,10) NOT NULL DEFAULT 0 CHECK (margin_used >= 0),
    unrealized_pnl NUMERIC(28,10) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('paper','live')),
    FOREIGN KEY (account_id,strategy_id,worker_id,mode)
      REFERENCES paper_trading.trading_accounts(account_id,strategy_id,worker_id,mode)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_positions_v2_leg ON paper_trading.positions_v2(account_id,trade_id);
CREATE INDEX IF NOT EXISTS ix_positions_v2_account_symbol ON paper_trading.positions_v2(account_id,symbol);
