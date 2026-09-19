-- SELECT-only role for the DevLift MCP query_data tool.
--
-- Run ONCE per database as a superuser / RDS master user, BEFORE or AFTER
-- migration 168_add_mcp_ro_views (the migration grants on mcp_ro.* whenever
-- the role already exists; if you run this afterwards, the GRANTs below cover
-- the views that exist today and ALTER DEFAULT PRIVILEGES covers new ones).
--
-- Then set DB_RO_USER=devlift_mcp_ro and DB_RO_PASSWORD=<the password> on the
-- obs_tool deployment. Without them the tool logs a warning and runs with the
-- application role; the SQL guard and READ ONLY transaction still apply, but
-- the role grant is the layer that makes "views only" a database fact.
--
-- Replace the password before running. Never commit a real one.

CREATE ROLE devlift_mcp_ro LOGIN PASSWORD 'CHANGE-ME' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

-- Read-only queries only; a runaway one is cancelled by the server-side
-- SET LOCAL statement_timeout as well, this is the outer bound.
ALTER ROLE devlift_mcp_ro SET default_transaction_read_only = on;
ALTER ROLE devlift_mcp_ro SET statement_timeout = '30s';
ALTER ROLE devlift_mcp_ro SET idle_in_transaction_session_timeout = '60s';

-- psql: \gexec runs the generated statement against the connected database.
SELECT format('GRANT CONNECT ON DATABASE %I TO devlift_mcp_ro', current_database()) \gexec
REVOKE ALL ON SCHEMA public FROM devlift_mcp_ro;

GRANT USAGE ON SCHEMA mcp_ro TO devlift_mcp_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA mcp_ro TO devlift_mcp_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA mcp_ro GRANT SELECT ON TABLES TO devlift_mcp_ro;

-- Verify: should list only mcp_ro views.
-- SELECT table_schema, table_name, privilege_type
-- FROM information_schema.role_table_grants WHERE grantee = 'devlift_mcp_ro';
