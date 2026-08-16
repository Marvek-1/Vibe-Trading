-- paper_cycle_events carries its own independent timeframe CHECK (migration
-- 004), separate from trading_accounts' -- 006 widened the latter but missed
-- this one. Every bounded_grid_v1 cycle write was failing this constraint
-- and rolling back the whole sync_tick transaction (which is why heartbeat
-- updates were also silently failing: the same transaction upserts the
-- heartbeat row, so a failed insert here meant no heartbeat row ever got
-- created either).
BEGIN;

ALTER TABLE paper_trading.paper_cycle_events
    DROP CONSTRAINT IF EXISTS paper_cycle_events_timeframe_check;
ALTER TABLE paper_trading.paper_cycle_events
    ADD CONSTRAINT paper_cycle_events_timeframe_check
    CHECK (timeframe IN ('5m', '10m', '15m', 'tick'));

COMMIT;
