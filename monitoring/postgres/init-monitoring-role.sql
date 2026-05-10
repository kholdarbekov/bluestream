-- MON-SUB-01: read-only monitoring role for postgres_exporter.
--
-- pg_monitor is a built-in role that grants the privileges
-- postgres_exporter needs (pg_read_all_settings, pg_read_all_stats,
-- pg_stat_scan_tables) without giving any data access. CONNECT on the app
-- DB is required because some exporter queries are per-database.
--
-- Usage (one-off, against the running postgres container):
--   ./scripts/manage-secrets.sh create postgres_monitoring_password
--   PASSWORD=$(cat secrets/postgres_monitoring_password)
--   docker compose exec -T postgres psql -U postgres -d "${POSTGRES_DB:-bluestream_db}" \
--     -v monitoring_password="$PASSWORD" \
--     -f - < monitoring/postgres/init-monitoring-role.sql
--
-- Idempotent: safe to re-run after rotating the password (UPDATE branch).

-- The role is created (or its password rotated) via dynamic SQL because
-- CREATE/ALTER ROLE … PASSWORD does not accept a parameter expression
-- directly; format() + EXECUTE substitutes it server-side as a literal.
DO
$body$
DECLARE
    v_password text := :'monitoring_password';
    v_dbname   text := current_database();
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'monitoring_ro') THEN
        EXECUTE format('CREATE ROLE monitoring_ro WITH LOGIN PASSWORD %L', v_password);
    ELSE
        EXECUTE format('ALTER ROLE monitoring_ro WITH LOGIN PASSWORD %L', v_password);
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO monitoring_ro', v_dbname);
END
$body$;

GRANT pg_monitor TO monitoring_ro;
