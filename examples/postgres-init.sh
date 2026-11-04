#!/bin/sh
set -eu
: "${MCP_DB_PASSWORD:?required}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -v mcp_password="$MCP_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE mcp_reader LOGIN PASSWORD %L', :'mcp_password') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_reader')\gexec
ALTER ROLE mcp_reader PASSWORD :'mcp_password';
GRANT CONNECT ON DATABASE agentdb TO mcp_reader;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname agentdb <<'SQL'
GRANT USAGE ON SCHEMA public TO mcp_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO mcp_reader;
DO $$
DECLARE
    target regprocedure;
BEGIN
    FOR target IN
        SELECT oid::regprocedure
        FROM pg_proc
        WHERE pronamespace = 'pg_catalog'::regnamespace
          AND proname IN ('pg_cancel_backend', 'pg_terminate_backend')
    LOOP
        EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC', target);
    END LOOP;
END
$$;
SQL
