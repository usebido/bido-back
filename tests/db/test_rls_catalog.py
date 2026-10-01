"""Run the CI RLS catalog check from the test suite, and prove it bites."""

import importlib.util
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.db

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_rls.py"

FindViolations = Callable[[AsyncConnection], Awaitable[list[str]]]


def _load_find_violations() -> FindViolations:
    spec = importlib.util.spec_from_file_location("check_rls", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    find_violations: FindViolations = module.find_violations
    return find_violations


find_violations = _load_find_violations()


async def test_schema_has_no_rls_violations(migrator_engine: AsyncEngine) -> None:
    async with migrator_engine.connect() as conn:
        assert await find_violations(conn) == []


async def test_check_flags_unprotected_and_unlisted_tables(migrator_engine: AsyncEngine) -> None:
    async with migrator_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(text("CREATE TABLE rls_probe_tenant (tenant_id uuid)"))
            await conn.execute(text("CREATE TABLE rls_probe_global (id int)"))
            await conn.execute(text("CREATE VIEW rls_probe_view AS SELECT 1 AS one"))
            violations = await find_violations(conn)
        finally:
            await trans.rollback()

    flagged = {v.split(":", 1)[0] for v in violations}
    assert flagged == {"rls_probe_tenant", "rls_probe_global", "rls_probe_view"}
