-- MON-SUB-01: read-only monitoring role for postgres_exporter.
--
-- pg_monitor is a built-in role that grants the privileges
-- postgres_exporter needs (pg_read_all_settings, pg_read_all_stats,
-- pg_stat_scan_tables) without giving any data access. CONNECT on the app
-- DB is required because some exporter queries are per-database.
--
-- Usage (one-off, against the running postgres container):
--   ./scripts/manage-secrets.sh create postgres_monitoring_password
--   docker compose exec -T postgres psql \
--       -U postgres -d "${POSTGRES_DB:-bluestream_db}" \
--       -v monitoring_password="$(cat secrets/postgres_monitoring_password)" \
--       -f - < monitoring/postgres/init-monitoring-role.sql
--
-- Idempotent: safe to re-run after rotating the password (CREATE branch
-- becomes a no-op; ALTER always sets the current password).
--
-- Why \gexec instead of a DO block: psql's :'var' substitution does NOT
-- happen inside dollar-quoted strings ($$...$$ / $tag$...$tag$), so the
-- password literal can't be referenced from a DO block. \gexec runs each
-- row of the preceding SELECT as a new query — we build the DDL with
-- format(%L) on the server (proper string-literal quoting) and let \gexec
-- execute the result.

-- (a) Create role only if missing. WHERE NOT EXISTS returns 0 rows when
--     the role is present, so \gexec runs nothing in that case.
SELECT format('CREATE ROLE monitoring_ro WITH LOGIN PASSWORD %L', :'monitoring_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'monitoring_ro')
\gexec

-- (b) Always set the password (covers both first install and rotate).
SELECT format('ALTER ROLE monitoring_ro WITH LOGIN PASSWORD %L', :'monitoring_password')
\gexec

-- (c) Grant pg_monitor membership (idempotent — PostgreSQL no-ops a re-grant).
GRANT pg_monitor TO monitoring_ro;

-- (d) Grant CONNECT on the currently-connected database. Using format(%I)
--     so the database name is properly identifier-quoted.
SELECT format('GRANT CONNECT ON DATABASE %I TO monitoring_ro', current_database())
\gexec
