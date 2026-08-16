-- Harden the already cut-over per-leg positions table without replacing it.
-- Current rows remain available throughout this additive migration.
BEGIN;

ALTER TABLE paper_trading.positions
    ADD COLUMN IF NOT EXISTS side TEXT,
    ADD COLUMN IF NOT EXISTS margin_mode TEXT,
    ADD COLUMN IF NOT EXISTS leverage INTEGER,
    ADD COLUMN IF NOT EXISTS isolated_margin NUMERIC(28,10),
    ADD COLUMN IF NOT EXISTS notional NUMERIC(36,18),
    ADD COLUMN IF NOT EXISTS entry_price NUMERIC(36,18),
    ADD COLUMN IF NOT EXISTS entry_time TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS take_profit_price NUMERIC(36,18),
    ADD COLUMN IF NOT EXISTS stop_loss_price NUMERIC(36,18),
    ADD COLUMN IF NOT EXISTS liquidation_price NUMERIC(36,18),
    ADD COLUMN IF NOT EXISTS entry_fee NUMERIC(28,10),
    ADD COLUMN IF NOT EXISTS entry_order_type TEXT,
    ADD COLUMN IF NOT EXISTS exit_order_type TEXT,
    ADD COLUMN IF NOT EXISTS max_hold_minutes INTEGER,
    ADD COLUMN IF NOT EXISTS accrued_funding NUMERIC(28,10) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_funding_ts TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS signal_reason TEXT,
    ADD COLUMN IF NOT EXISTS market_regime TEXT,
    ADD COLUMN IF NOT EXISTS grid_level INTEGER;

-- The v2 unique index has already proven every active row is non-null and
-- unique. Promote that exact index; no table rewrite or lossy aggregation.
ALTER TABLE paper_trading.positions
    ADD CONSTRAINT positions_leg_pkey PRIMARY KEY
    USING INDEX uq_positions_v2_leg;

COMMIT;
