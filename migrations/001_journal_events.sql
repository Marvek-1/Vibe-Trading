-- 001_journal_events.sql
-- Append-only trade journal, living in the idim_ikang database as its own
-- "journal" schema (a data pocket, not a second database -- idimikang,
-- vibe-trading, and the journal all share one Postgres instance per
-- deploy direction).
--
-- Non-negotiable: the journal_writer role gets INSERT + SELECT only.
-- No UPDATE/DELETE grant exists for it to misuse -- enforced by Postgres
-- itself, not by convention. Requires a superuser (postgres) to run the
-- CREATE ROLE statement; everything else is ownable by idona.

CREATE SCHEMA IF NOT EXISTS journal;

CREATE TABLE IF NOT EXISTS journal.journal_events (
    id          UUID PRIMARY KEY,
    ts_utc      DOUBLE PRECISION NOT NULL,
    event_type  TEXT NOT NULL CHECK (event_type IN ('SIGNAL','ENTRY','EXIT','NOTE')),
    symbol      TEXT NOT NULL,
    exchange    TEXT,
    side        TEXT CHECK (side IN ('LONG','SHORT') OR side IS NULL),
    price       DOUBLE PRECISION,
    qty         DOUBLE PRECISION,
    ref_id      UUID,             -- links EXIT/NOTE back to an ENTRY/SIGNAL id
    detail      TEXT,             -- free text / JSON blob
    attested_by TEXT NOT NULL,
    CONSTRAINT journal_events_attested_by_not_blank CHECK (attested_by <> '')
);

CREATE INDEX IF NOT EXISTS idx_journal_events_symbol ON journal.journal_events(symbol);
CREATE INDEX IF NOT EXISTS idx_journal_events_type   ON journal.journal_events(event_type);
CREATE INDEX IF NOT EXISTS idx_journal_events_ts     ON journal.journal_events(ts_utc);
CREATE INDEX IF NOT EXISTS idx_journal_events_ref_id ON journal.journal_events(ref_id);

CREATE TABLE IF NOT EXISTS journal.watchlist (
    symbol   TEXT NOT NULL,
    exchange TEXT NOT NULL,
    screener TEXT NOT NULL,
    added_ts DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (symbol, exchange)
);

-- Writer role creation is a SEPARATE file (001b_create_journal_writer_role.sql)
-- run quietly (no statement echo) so the password literal is never printed
-- to any log or transcript -- see run_001_journal_events.sh.

GRANT USAGE ON SCHEMA journal TO journal_writer;
GRANT INSERT, SELECT ON journal.journal_events TO journal_writer;
GRANT INSERT, SELECT ON journal.watchlist TO journal_writer;
-- Belt-and-suspenders: explicit revoke, even though these were never granted.
REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON journal.journal_events FROM journal_writer;
REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON journal.watchlist FROM journal_writer;

-- Witness receipt: the actual column list of the table this migration created.
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'journal' AND table_name = 'journal_events'
ORDER BY ordinal_position;
