ALTER TABLE paper_trading.paper_cycle_events
    ADD COLUMN IF NOT EXISTS signal_score NUMERIC,
    ADD COLUMN IF NOT EXISTS entry_threshold NUMERIC,
    ADD COLUMN IF NOT EXISTS market_data_age NUMERIC,
    ADD COLUMN IF NOT EXISTS volatility NUMERIC,
    ADD COLUMN IF NOT EXISTS spread NUMERIC,
    ADD COLUMN IF NOT EXISTS risk_rejection_reason TEXT,
    ADD COLUMN IF NOT EXISTS strategy_rejection_reason TEXT,
    ADD COLUMN IF NOT EXISTS order_rejection_reason TEXT;
