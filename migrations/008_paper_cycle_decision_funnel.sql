-- Per-cycle observability for the paper strategy -> execution funnel.
-- A JSONB payload keeps this additive and lets each strategy report only the
-- counters it can truthfully measure without inventing placeholder columns.
ALTER TABLE paper_trading.paper_cycle_events
    ADD COLUMN IF NOT EXISTS decision_funnel JSONB NOT NULL DEFAULT '{}'::jsonb;
