"""Fail if any table in `public` escapes the tenant-isolation rules.

Rules (enforced in CI after `alembic upgrade head`):
  * Every table with a `tenant_id` column, plus `tenants` itself, must have
    RLS enabled, RLS forced, and at least one policy.
  * Every other table must be listed in GLOBAL_TABLES. A new table without
    tenant_id fails until someone consciously decides it is global.
  * Views must be `security_invoker` (otherwise they run as their owner and
    skip the caller's RLS) or be listed in GLOBAL_TABLES.

Connects with MIGRATION_DATABASE_URL, falling back to DATABASE_URL; only
reads pg_catalog. Exit code 1 on any violation.

Usage: uv run python scripts/check_rls.py
"""

import asyncio
import sys
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from bido_back.settings import get_settings

SCHEMA = "public"
TENANT_COLUMN = "tenant_id"
# Tables that are tenant-scoped without a tenant_id column (keyed by id).
TENANT_ROOT_TABLES = frozenset({"tenants"})
# Tables deliberately NOT tenant-scoped. Adding to this list is a security
# decision: say why next to the entry.
GLOBAL_TABLES = frozenset(
    {
        "users",  # a person can belong to several tenants; access goes via memberships
        "alembic_version",  # migration bookkeeping; app role has no grants on it
    }
)

_CATALOG_QUERY = text(
    """
    SELECT
        c.relname AS name,
        c.relkind::text AS kind,
        c.relrowsecurity AS rls_enabled,
        c.relforcerowsecurity AS rls_forced,
        (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policy_count,
        EXISTS (
            SELECT 1 FROM pg_attribute a
            WHERE a.attrelid = c.oid
              AND a.attname = :tenant_column
              AND a.attnum > 0
              AND NOT a.attisdropped
        ) AS has_tenant_id,
        coalesce(
            c.reloptions @> ARRAY['security_invoker=true']
            OR c.reloptions @> ARRAY['security_invoker=on'],
            false
        ) AS security_invoker
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = :schema
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
    ORDER BY c.relname
    """
)


@dataclass(frozen=True)
class Relation:
    name: str
    kind: str
    rls_enabled: bool
    rls_forced: bool
    policy_count: int
    has_tenant_id: bool
    security_invoker: bool


async def load_relations(conn: AsyncConnection) -> list[Relation]:
    result = await conn.execute(_CATALOG_QUERY, {"schema": SCHEMA, "tenant_column": TENANT_COLUMN})
    return [
        Relation(
            name=row.name,
            kind=row.kind,
            rls_enabled=row.rls_enabled,
            rls_forced=row.rls_forced,
            policy_count=int(row.policy_count),
            has_tenant_id=row.has_tenant_id,
            security_invoker=row.security_invoker,
        )
        for row in result
    ]


def evaluate(relations: list[Relation]) -> list[str]:
    """Return one human-readable violation per broken rule."""
    violations: list[str] = []
    for rel in relations:
        tenant_scoped = rel.has_tenant_id or rel.name in TENANT_ROOT_TABLES

        if rel.kind in ("v", "m"):
            # Materialized views cannot carry RLS at all; plain views skip the
            # caller's RLS unless they are security_invoker.
            safe = rel.kind == "v" and rel.security_invoker
            if not safe and rel.name not in GLOBAL_TABLES:
                violations.append(
                    f"{rel.name}: {'materialized view' if rel.kind == 'm' else 'view'} "
                    "bypasses the caller's RLS; use a security_invoker view or "
                    "allowlist it in GLOBAL_TABLES with a reason"
                )
            continue

        if tenant_scoped:
            missing: list[str] = []
            if not rel.rls_enabled:
                missing.append("ENABLE ROW LEVEL SECURITY")
            if not rel.rls_forced:
                missing.append("FORCE ROW LEVEL SECURITY")
            if rel.policy_count < 1:
                missing.append("at least one policy")
            if missing:
                violations.append(f"{rel.name}: tenant-scoped but missing {', '.join(missing)}")
        elif rel.name not in GLOBAL_TABLES:
            violations.append(
                f"{rel.name}: no {TENANT_COLUMN} and not in GLOBAL_TABLES; add "
                f"{TENANT_COLUMN} + RLS, or allowlist it with a reason"
            )
    return violations


async def find_violations(conn: AsyncConnection) -> list[str]:
    return evaluate(await load_relations(conn))


def _report(relations: list[Relation], violations: list[str]) -> str:
    lines = [f"RLS check on schema '{SCHEMA}' ({len(relations)} relations)"]
    for rel in relations:
        if rel.name in GLOBAL_TABLES:
            status = "global (allowlisted)"
        elif rel.kind in ("v", "m"):
            status = "view, security_invoker" if rel.security_invoker else "view"
        else:
            status = (
                f"rls={'on' if rel.rls_enabled else 'OFF'} "
                f"force={'on' if rel.rls_forced else 'OFF'} "
                f"policies={rel.policy_count}"
            )
        lines.append(f"  {rel.name:<32} {status}")
    if violations:
        lines.append(f"FAIL: {len(violations)} violation(s)")
        lines.extend(f"  - {v}" for v in violations)
    else:
        lines.append("OK: no violations")
    return "\n".join(lines)


async def main() -> int:
    settings = get_settings()
    url = settings.migration_database_url or settings.database_url
    engine = create_async_engine(str(url))
    try:
        async with engine.connect() as conn:
            relations = await load_relations(conn)
    finally:
        await engine.dispose()
    violations = evaluate(relations)
    print(_report(relations, violations))
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
