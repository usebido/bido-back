-- Local and CI bootstrap for the `bido` database. Runs once, as the
-- superuser, from /docker-entrypoint-initdb.d/ (docker compose) or via psql
-- (CI service container).
--
-- !! LOCAL / CI ONLY !!
-- The passwords below ('migrator', 'app') exist only for the throwaway local
-- and CI databases. In AWS the roles are created by infrastructure code and
-- authenticate through Secrets Manager-managed credentials or RDS IAM auth.
-- Never reuse these values anywhere else.
--
-- Two roles, two jobs:
--   bido_migrator  owns the `public` schema and every table in it. Used only by
--                  Alembic (MIGRATION_DATABASE_URL). Not a superuser.
--   bido_app       used by the API and the worker (DATABASE_URL). Owns
--                  nothing, cannot CREATE, cannot bypass RLS. Gets DML on
--                  tables only through the default privileges below.
--
-- The script is idempotent so CI can re-run it against a reused container.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bido_migrator') THEN
        CREATE ROLE bido_migrator LOGIN PASSWORD 'migrator'
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bido_app') THEN
        CREATE ROLE bido_app LOGIN PASSWORD 'app'
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

-- Extensions need superuser on stock Postgres, so they are installed here
-- rather than in a migration.
CREATE EXTENSION IF NOT EXISTS vector;

-- The migrator owns the schema; nobody else may create objects in it.
ALTER SCHEMA public OWNER TO bido_migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO bido_app;

GRANT CONNECT ON DATABASE bido TO bido_migrator, bido_app;

-- Every table/sequence the migrator creates is automatically usable (DML
-- only) by the app role. Narrower grants (e.g. append-only audit_events)
-- are applied per table in the migrations with REVOKE.
ALTER DEFAULT PRIVILEGES FOR ROLE bido_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bido_app;
ALTER DEFAULT PRIVILEGES FOR ROLE bido_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO bido_app;
