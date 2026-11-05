\getenv mcp_password MCP_DB_PASSWORD
SELECT format('CREATE ROLE mcp_reader LOGIN PASSWORD %L', :'mcp_password') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_reader')\gexec
ALTER ROLE mcp_reader PASSWORD :'mcp_password';
ALTER ROLE mcp_reader SET default_transaction_read_only = on;
DO $$
DECLARE
    database_name text;
BEGIN
    FOR database_name IN
        SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate
    LOOP
        EXECUTE format('GRANT CONNECT ON DATABASE %I TO mcp_reader', database_name);
    END LOOP;
END
$$;
DO $$
DECLARE
    schema_name text;
BEGIN
    FOR schema_name IN
        SELECT nspname FROM pg_namespace
        WHERE nspname <> 'information_schema' AND nspname NOT LIKE 'pg_%'
    LOOP
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO mcp_reader', schema_name);
        EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO mcp_reader', schema_name);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA %I GRANT SELECT ON TABLES TO mcp_reader', schema_name);
    END LOOP;
END
$$;
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
