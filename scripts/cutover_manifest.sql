-- =========================================================================
-- cutover_manifest.sql - psql-only evidence capture for idim_ikang cutover.
-- Read-only: SELECT and catalog reads only. No DDL, DML, or VACUUM.
-- Run identically on frozen Dell and restored VPS, then diff the outputs.
-- =========================================================================

\set ON_ERROR_STOP on
\pset format unaligned
\pset fieldsep '|'
\pset footer off
\pset pager off
\pset null '(null)'
\timing off

SET TIME ZONE 'UTC';
SET client_min_messages = warning;
SET statement_timeout = '10min';

\echo '### 00 IDENTITY'
SELECT current_database() AS db, version() AS server;

\echo ''
\echo '### 01 ENCODING / COLLATION / PROVIDER  [abort: any mismatch]'
SELECT pg_encoding_to_char(d.encoding) AS encoding,
       d.datcollate,
       d.datctype,
       to_jsonb(d.*) ->> 'datlocprovider' AS loc_provider,
       to_jsonb(d.*) ->> 'daticulocale' AS icu_locale,
       to_jsonb(d.*) ->> 'datcollversion' AS coll_version
  FROM pg_database d
 WHERE d.datname = current_database();

\echo ''
\echo '### 02 SEMANTIC SETTINGS  [abort: semantic setting diff]'
SELECT name, setting
  FROM pg_settings
 WHERE name IN ('TimeZone','DateStyle','IntervalStyle','extra_float_digits',
                'lc_numeric','lc_monetary','server_encoding','server_version',
                'standard_conforming_strings','default_transaction_isolation')
 ORDER BY name;

\echo ''
\echo '### 03 SCHEMAS PRESENT'
SELECT nspname
  FROM pg_namespace
 WHERE nspname NOT LIKE 'pg\_%'
   AND nspname <> 'information_schema'
 ORDER BY 1;

\echo ''
\echo '### 04 TABLE INVENTORY + EXACT ROW COUNTS  [abort: any mismatch]'
SELECT n.nspname AS schema,
       c.relname AS table_name,
       (xpath('/row/cnt/text()',
              query_to_xml(format('SELECT count(*) AS cnt FROM %I.%I',
                                  n.nspname, c.relname),
                           false, true, '')))[1]::text::bigint AS exact_rows
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = 'r'
   AND n.nspname NOT LIKE 'pg\_%'
   AND n.nspname <> 'information_schema'
 ORDER BY 1, 2;

\echo ''
\echo '### 05 SEQUENCES vs MAX REFERENCED VALUE  [abort: last_value below max]'
SELECT s.seqrelid::regclass::text AS sequence_name,
       pg_sequence_last_value(s.seqrelid) AS last_value,
       (n.nspname || '.' || t.relname) AS owned_table,
       a.attname AS owned_column,
       (xpath('/row/m/text()',
              query_to_xml(format('SELECT max(%I) AS m FROM %I.%I',
                                  a.attname, n.nspname, t.relname),
                           false, true, '')))[1]::text::bigint AS max_referenced
  FROM pg_sequence s
  JOIN pg_depend d ON d.objid = s.seqrelid
                   AND d.deptype = 'a'
                   AND d.classid = 'pg_class'::regclass
  JOIN pg_class t ON t.oid = d.refobjid
  JOIN pg_attribute a ON a.attrelid = t.oid
                     AND a.attnum = d.refobjsubid
  JOIN pg_namespace n ON n.oid = t.relnamespace
 WHERE n.nspname NOT LIKE 'pg\_%'
 ORDER BY 1;

\echo ''
\echo '### 06 INDEX VALIDITY  [abort: any not-valid / not-ready / not-live]'
SELECT (n.nspname || '.' || t.relname) AS table_name,
       ic.relname AS index_name,
       i.indisvalid, i.indisready, i.indislive, i.indisunique, i.indisprimary
  FROM pg_index i
  JOIN pg_class ic ON ic.oid = i.indexrelid
  JOIN pg_class t ON t.oid = i.indrelid
  JOIN pg_namespace n ON n.oid = t.relnamespace
 WHERE n.nspname NOT LIKE 'pg\_%'
   AND n.nspname <> 'information_schema'
   AND NOT (i.indisvalid AND i.indisready AND i.indislive)
 ORDER BY 1, 2;
\echo '(empty above = all indexes valid)'

\echo ''
\echo '### 07 CONSTRAINTS - IDENTITY KEYS  [abort: definition change]'
SELECT (n.nspname || '.' || c.relname) AS table_name,
       con.conname,
       con.contype,
       con.convalidated,
       pg_get_constraintdef(con.oid) AS definition
  FROM pg_constraint con
  JOIN pg_class c ON c.oid = con.conrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname NOT LIKE 'pg\_%'
   AND n.nspname <> 'information_schema'
 ORDER BY 1, 2;

\echo ''
\echo '### 08 UNVALIDATED CONSTRAINTS  [abort: unexpected NOT VALID]'
-- Expected baseline exception for this release:
--   paper_trading.trading_accounts.live_mode_release_lock
-- It deliberately prevents new live-mode rows while remaining NOT VALID so
-- legacy rows can be inspected without weakening enforcement on new writes.
SELECT conrelid::regclass::text AS table_name,
       conname,
       contype,
       pg_get_constraintdef(oid) AS definition
  FROM pg_constraint
 WHERE NOT convalidated
   AND connamespace NOT IN ('pg_catalog'::regnamespace,
                            'information_schema'::regnamespace)
 ORDER BY 1, 2;
\echo '(empty above = every constraint fully validated)'

\echo ''
\echo '### 09 PER-ACCOUNT MANIFEST  [abort: any scoped count mismatch]'
SELECT format(
  'SELECT %L AS scope_col, %L AS tbl, %I::text AS scope_val, count(*) AS n '
  'FROM %I.%I GROUP BY 3 ORDER BY 3;',
  a.attname, (n.nspname||'.'||c.relname), a.attname,
  n.nspname, c.relname)
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid
                     AND a.attnum > 0
                     AND NOT a.attisdropped
 WHERE c.relkind = 'r'
   AND n.nspname NOT LIKE 'pg\_%'
   AND n.nspname <> 'information_schema'
   AND a.attname IN ('account_id','worker_id','session_id')
 ORDER BY n.nspname, c.relname, a.attname
\gexec

\echo ''
\echo '### 10 MAX EVENT TIMESTAMP PER TABLE  [freshness / truncation]'
SELECT format(
  'SELECT %L AS tbl, %L AS col, max(%I)::text AS max_ts, '
  'min(%I)::text AS min_ts FROM %I.%I;',
  (n.nspname||'.'||c.relname), a.attname, a.attname, a.attname,
  n.nspname, c.relname)
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid
                     AND a.attnum > 0
                     AND NOT a.attisdropped
  JOIN pg_type ty ON ty.oid = a.atttypid
 WHERE c.relkind = 'r'
   AND n.nspname NOT LIKE 'pg\_%'
   AND n.nspname <> 'information_schema'
   AND ty.typname IN ('timestamptz','timestamp')
   AND a.attname IN ('created_at','filled_at','observed_at','updated_at',
                     'last_seen_at','last_trade_at','event_ts','ts')
 ORDER BY n.nspname, c.relname, a.attname
\gexec

\echo ''
\echo '### 11 POSITIONS + ACCOUNTS - FULL JSON  [abort: any field differs]'
-- The dashboard aggregates multiple grid legs into one display position.
-- Never use the panel as cutover evidence: compare every JSON row here.
-- The rehearsal found six current position legs (three per grid account), but
-- the authoritative expected set is whatever this section captures frozen.
SELECT format(
  'SELECT %L AS tbl, row_to_json(t)::text AS row FROM %I.%I t ORDER BY 2;',
  (n.nspname||'.'||c.relname), n.nspname, c.relname)
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = 'r'
   AND n.nspname NOT LIKE 'pg\_%'
   AND n.nspname <> 'information_schema'
   AND (c.relname ~ 'position'
        OR c.relname ~ 'trading_account'
        OR c.relname ~ 'heartbeat'
        OR c.relname ~ 'registry')
 ORDER BY 1
\gexec

\echo ''
\echo '### 12 ORPHAN SCAN - scoped rows with no FK parent'
SELECT format(
  'SELECT %L AS child, count(*) AS orphans FROM %I.%I ch '
  'WHERE ch.%I IS NOT NULL AND NOT EXISTS ('
  'SELECT 1 FROM %I.%I pa WHERE pa.%I = ch.%I);',
  (cn.nspname||'.'||cc.relname||'.'||ca.attname),
  cn.nspname, cc.relname, ca.attname,
  pn.nspname, pc.relname, pa.attname, ca.attname)
  FROM pg_constraint con
  JOIN pg_class cc ON cc.oid = con.conrelid
  JOIN pg_namespace cn ON cn.oid = cc.relnamespace
  JOIN pg_attribute ca ON ca.attrelid = cc.oid
                      AND ca.attnum = con.conkey[1]
  JOIN pg_class pc ON pc.oid = con.confrelid
  JOIN pg_namespace pn ON pn.oid = pc.relnamespace
  JOIN pg_attribute pa ON pa.attrelid = pc.oid
                      AND pa.attnum = con.confkey[1]
 WHERE con.contype = 'f'
   AND array_length(con.conkey, 1) = 1
   AND cn.nspname NOT LIKE 'pg\_%'
 ORDER BY 1
\gexec
\echo '(all orphan counts must be 0)'

\echo ''
\echo '### 13 LIVE CONNECTIONS  [Dell freeze: must be empty]'
SELECT coalesce(host(client_addr),'local') AS client,
       application_name,
       state,
       count(*) AS n
  FROM pg_stat_activity
 WHERE datname = current_database()
   AND pid <> pg_backend_pid()
   AND backend_type = 'client backend'
 GROUP BY 1,2,3
 ORDER BY 4 DESC;
\echo '(empty above = quiescent)'

\echo ''
\echo '### 14 IDLE IN TRANSACTION  [must be 0 in steady VPS operation]'
SELECT count(*) AS idle_in_transaction
  FROM pg_stat_activity
 WHERE datname = current_database()
   AND state = 'idle in transaction';

\echo ''
\echo '### 15 XMIN HORIZON / BLOAT RISK'
SELECT max(age(backend_xmin)) AS oldest_backend_xmin_age
  FROM pg_stat_activity
 WHERE backend_xmin IS NOT NULL;

\echo ''
\echo '### 16 CAPTURE TIME'
SELECT now() AT TIME ZONE 'UTC' AS captured_utc_do_not_compare;
