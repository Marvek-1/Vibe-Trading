-- Complete the account scope on every mutable paper-trading relation.
BEGIN;

ALTER TABLE paper_trading.trading_accounts
    ADD COLUMN IF NOT EXISTS margin_used NUMERIC(28,10) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS unrealized_pnl NUMERIC(28,10) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS current_equity NUMERIC(28,10) NOT NULL DEFAULT 0;

UPDATE paper_trading.trading_accounts
   SET current_equity = initial_capital + realized_pnl + unrealized_pnl + funding_pnl - fees
 WHERE current_equity = 0;

CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_account
    ON paper_trading.trading_accounts(worker_id, mode);
CREATE UNIQUE INDEX IF NOT EXISTS uq_account_identity
    ON paper_trading.trading_accounts(account_id, strategy_id, worker_id, mode);
CREATE UNIQUE INDEX IF NOT EXISTS uq_order_scope
    ON paper_trading.orders(account_id, exchange_order_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_fill_scope
    ON paper_trading.fills(account_id, exchange_fill_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_position_scope
    ON paper_trading.positions(account_id, symbol);

DO $$
DECLARE relation_name text;
BEGIN
    FOREACH relation_name IN ARRAY ARRAY[
        'orders','fills','positions','funding_events','equity_history','worker_heartbeats'
    ] LOOP
        EXECUTE format(
            'ALTER TABLE paper_trading.%I ADD COLUMN IF NOT EXISTS mode TEXT',
            relation_name
        );
        EXECUTE format('UPDATE paper_trading.%I SET mode = ''paper'' WHERE mode IS NULL', relation_name);
        EXECUTE format('ALTER TABLE paper_trading.%I ALTER COLUMN mode SET NOT NULL', relation_name);
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = relation_name || '_mode_allowed'
               AND conrelid = ('paper_trading.' || relation_name)::regclass
        ) THEN
            EXECUTE format(
                'ALTER TABLE paper_trading.%I ADD CONSTRAINT %I CHECK (mode IN (''paper'',''live''))',
                relation_name, relation_name || '_mode_allowed'
            );
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = relation_name || '_account_identity_fk'
               AND conrelid = ('paper_trading.' || relation_name)::regclass
        ) THEN
            EXECUTE format(
                'ALTER TABLE paper_trading.%I ADD CONSTRAINT %I FOREIGN KEY '
                || '(account_id,strategy_id,worker_id,mode) REFERENCES '
                || 'paper_trading.trading_accounts(account_id,strategy_id,worker_id,mode)',
                relation_name, relation_name || '_account_identity_fk'
            );
        END IF;
    END LOOP;
END $$;

ALTER TABLE paper_trading.trading_accounts
    DROP CONSTRAINT IF EXISTS account_equity_reconciles;
ALTER TABLE paper_trading.trading_accounts
    ADD CONSTRAINT account_equity_reconciles CHECK (
        abs(current_equity - (
            initial_capital + realized_pnl + unrealized_pnl + funding_pnl - fees
        )) < 0.000001
    );

COMMIT;
