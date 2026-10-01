-- Local and CI bootstrap for the `bido` database. Runs once, as the
-- superuser, from /docker-entrypoint-initdb.d/ (docker compose) or via psql
-- (CI service container).
--
-- !! LOCAL / CI ONLY !!
-- The passwords below ('migrator', 'app', 'agno') exist only for the throwaway local
-- and CI databases. In AWS the roles are created by infrastructure code and
-- authenticate through Secrets Manager-managed credentials or RDS IAM auth.
-- Never reuse these values anywhere else.
--
-- Three roles, three jobs:
--   bido_migrator  owns the `public` schema and every table in it. Used only by
--                  Alembic (MIGRATION_DATABASE_URL). Not a superuser.
--   bido_app       used by the API and the worker (DATABASE_URL). Owns
--                  nothing, cannot CREATE, cannot bypass RLS. Gets DML on
--                  tables only through the default privileges below.
--   bido_agno      used only by the Agno framework (AGNO_DATABASE_URL) for its
--                  sessions, memory and runs. Owns the `agno` schema and may
--                  create objects there and nowhere else. Has no access at all
--                  to schema `public` (no USAGE), so it cannot read or write a
--                  business table even by mistake. Agno's tables carry no
--                  tenant_id and no RLS: tenant separation there comes from
--                  this role boundary plus tenant-namespaced user_id /
--                  session_id values, and check_rls.py ignores the schema.
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

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bido_agno') THEN
        CREATE ROLE bido_agno LOGIN PASSWORD 'agno'
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

-- Extensions need superuser on stock Postgres, so they are installed here
-- rather than in a migration.
CREATE EXTENSION IF NOT EXISTS vector;

-- The migrator owns the schema; nobody else may create objects in it.
ALTER SCHEMA public OWNER TO bido_migrator;
-- USAGE is revoked from PUBLIC too (Postgres grants it by default), so a role
-- reaches `public` only through an explicit grant. bido_agno has none.
REVOKE CREATE, USAGE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO bido_app;

GRANT CONNECT ON DATABASE bido TO bido_migrator, bido_app, bido_agno;

-- Agno's own schema. bido_agno owns it, so it can create its tables there;
-- nobody else gets any privilege on it (a new schema grants nothing to
-- PUBLIC), so bido_app and bido_migrator cannot read Agno's data. The schema
-- is created here, not by Agno: bido_agno has no CREATE on the database, so
-- Agno must be configured with create_schema=False.
-- Agno's session/memory tables need nothing from `public`. If Agno Knowledge
-- (PgVector) is adopted later it needs the `vector` type, which lives in
-- `public`: then GRANT USAGE ON SCHEMA public TO bido_agno, and still no
-- table privileges.
CREATE SCHEMA IF NOT EXISTS agno AUTHORIZATION bido_agno;
ALTER SCHEMA agno OWNER TO bido_agno;
REVOKE ALL ON SCHEMA agno FROM PUBLIC, bido_app, bido_migrator;
REVOKE ALL ON SCHEMA public FROM bido_agno;
-- Unqualified names resolve to `agno` only, never to `public`.
ALTER ROLE bido_agno SET search_path = agno;

-- Every table/sequence the migrator creates is automatically usable (DML
-- only) by the app role. Narrower grants (e.g. append-only audit_events)
-- are applied per table in the migrations with REVOKE.
ALTER DEFAULT PRIVILEGES FOR ROLE bido_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bido_app;
ALTER DEFAULT PRIVILEGES FOR ROLE bido_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO bido_app;
