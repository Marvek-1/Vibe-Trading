BEGIN;
CREATE TABLE IF NOT EXISTS paper_trading.paper_cycle_events (
    id UUID PRIMARY KEY,
    account_id UUID NOT NULL REFERENCES paper_trading.trading_accounts(account_id),
    worker_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    timeframe TEXT NOT NULL CHECK (timeframe IN ('5m','10m','15m')),
    cycle_started_at TIMESTAMPTZ NOT NULL,
    cycle_completed_at TIMESTAMPTZ NOT NULL,
    market_data_source TEXT NOT NULL,
    market_data_fresh BOOLEAN NOT NULL,
    orders_created INTEGER NOT NULL DEFAULT 0,
    fills_created INTEGER NOT NULL DEFAULT 0,
    trades_closed INTEGER NOT NULL DEFAULT 0,
    realized_pnl NUMERIC(28,10) NOT NULL DEFAULT 0,
    unrealized_pnl NUMERIC(28,10) NOT NULL DEFAULT 0,
    fees NUMERIC(28,10) NOT NULL DEFAULT 0,
    funding_pnl NUMERIC(28,10) NOT NULL DEFAULT 0,
    ending_equity NUMERIC(28,10) NOT NULL,
    ledger_reconciled BOOLEAN NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('COMPLETED','DEGRADED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(worker_id, cycle_completed_at)
);
CREATE TABLE IF NOT EXISTS paper_trading.trading_notifications (
    id UUID PRIMARY KEY,
    account_id UUID NOT NULL REFERENCES paper_trading.trading_accounts(account_id),
    worker_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('CYCLE_COMPLETE','PROFIT_BANKED','LOSS_BOOKED','NO_TRADE','WORKER_DEGRADED','LEDGER_FAILURE','STALE_MARKET_DATA')),
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    source_event_id UUID NOT NULL REFERENCES paper_trading.paper_cycle_events(id),
    acknowledged BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(source_event_id,event_type)
);
COMMIT;
