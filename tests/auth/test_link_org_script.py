"""scripts/link_demo_org.py: who may run it where, and what a run leaves behind.

The script runs as an operator would run it, in a fresh interpreter with
ENVIRONMENT set. The refusals need no database: they point the script at an
unreachable one, so reaching it would fail with a different error. The db tests
link real organizations and are cleaned up by `created` (tests/auth/test_orgs.py).
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import AuditEvent, Tenant
from bido_back.orgs import tenant_for_org
from tests.auth.test_orgs import Created, created
from tests.db.conftest import app_engine, migrator_engine

__all__ = ["app_engine", "created", "migrator_engine"]

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "link_demo_org.py"
UNREACHABLE = "postgresql+asyncpg://nobody:nothing@127.0.0.1:9/nothing"


def _run(environment: str, *args: str, database: bool = False) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("CLERK_ALLOWED_ORG_IDS", "AUTH_AUTO_PROVISION")
    }
    env["ENVIRONMENT"] = environment
    if not database:
        env["DATABASE_URL"] = UNREACHABLE
        env["MIGRATION_DATABASE_URL"] = UNREACHABLE
    return subprocess.run(  # noqa: S603 - fixed argv, our own interpreter and script
        [sys.executable, str(SCRIPT), *args],
        # Not the repo root: the child must not read a developer's .env.
        cwd=tempfile.gettempdir(),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _org() -> str:
    return f"org_test_{uuid4().hex}"


# --- Refusals (no database) -----------------------------------------------------------


@pytest.mark.parametrize(
    ("environment", "args", "message"),
    [
        pytest.param(
            "prd",
            ["--company-name", "Acme", "--operator", "ops"],
            "pass --confirm-environment prd",
            id="prd-without-confirmation",
        ),
        pytest.param(
            "prd",
            ["--confirm-environment", "dev", "--company-name", "Acme", "--operator", "ops"],
            "does not match ENVIRONMENT=prd",
            id="prd-confirmed-as-dev",
        ),
        pytest.param(
            "dev",
            ["--confirm-environment", "prd", "--company-name", "Acme"],
            "does not match ENVIRONMENT=dev",
            id="dev-confirmed-as-prd",
        ),
        pytest.param(
            "prd",
            ["--confirm-environment", "prd", "--tenant-id", str(uuid4()), "--operator", "ops"],
            "name the company with --company-name",
            id="prd-existing-tenant",
        ),
        pytest.param(
            "prd",
            ["--confirm-environment", "prd", "--with-motor-demo", "--operator", "ops"],
            "name the company with --company-name",
            id="prd-motor-demo",
        ),
        pytest.param(
            "prd",
            ["--confirm-environment", "prd", "--company-name", "Acme"],
            "--operator",
            id="prd-without-operator",
        ),
        pytest.param(
            "prd",
            ["--confirm-environment", "prd", "--company-name", "  ", "--operator", "ops"],
            "--company-name must be 1 to 200 characters",
            id="prd-blank-company-name",
        ),
        pytest.param(
            "test",
            ["--company-name", "Acme"],
            "ENVIRONMENT=local, dev or prd",
            id="other-environment",
        ),
    ],
)
def test_refuses_anything_but_a_deliberate_run(
    environment: str, args: list[str], message: str
) -> None:
    result = _run(environment, "--clerk-org-id", _org(), *args)

    assert result.returncode == 1, result.stderr
    assert message in result.stderr
    assert result.stdout == ""


def test_prd_needs_a_target() -> None:
    result = _run("prd", "--clerk-org-id", _org(), "--confirm-environment", "prd")

    assert result.returncode == 2
    assert "--company-name" in result.stderr


# --- Linking (database) ---------------------------------------------------------------


async def _linked_tenant(app_engine: AsyncEngine, org_id: str) -> Tenant:
    tenant_id = await tenant_for_org(create_sessionmaker(app_engine), org_id)
    assert tenant_id is not None
    async with tenant_session(app_engine, tenant_id) as session:
        tenant = await session.get(Tenant, tenant_id)
    assert tenant is not None
    return tenant


async def _link_events(app_engine: AsyncEngine, tenant: Tenant) -> list[dict[str, object]]:
    async with tenant_session(app_engine, tenant.id) as session:
        rows = await session.scalars(
            select(AuditEvent.payload).where(
                AuditEvent.tenant_id == tenant.id, AuditEvent.action == "tenant.linked"
            )
        )
        return list(rows)


@pytest.mark.db
async def test_prd_links_a_named_company_once_and_records_who(
    app_engine: AsyncEngine, created: Created
) -> None:
    org_id = created.org()
    prd = ("--confirm-environment", "prd", "--clerk-org-id", org_id, "--operator", "ops")

    first = _run("prd", *prd, "--company-name", "Pilot S.p.A.", database=True)
    assert first.returncode == 0, first.stderr
    tenant = await _linked_tenant(app_engine, org_id)
    assert tenant.name == "Pilot S.p.A."
    assert f"tenant {tenant.id}" in first.stdout
    assert await _link_events(app_engine, tenant) == [
        {"source": "operator", "operator": "ops", "clerk_org_id": org_id, "created": True}
    ]

    again = _run("prd", *prd, "--company-name", "Pilot S.p.A.", database=True)
    assert again.returncode == 0, again.stderr
    assert again.stdout.startswith("unchanged")
    assert (await _linked_tenant(app_engine, org_id)).id == tenant.id
    assert len(await _link_events(app_engine, tenant)) == 1

    other = _run("prd", *prd, "--company-name", "Another S.p.A.", database=True)
    assert other.returncode == 1
    assert "already linked to another company" in other.stderr
    assert (await _linked_tenant(app_engine, org_id)).name == "Pilot S.p.A."
    assert len(await _link_events(app_engine, tenant)) == 1


@pytest.mark.db
async def test_dev_links_without_confirmation_as_before(
    app_engine: AsyncEngine, created: Created
) -> None:
    # --name and the local user as operator, no --confirm-environment: unchanged in dev.
    new_org = created.org()
    result = _run("dev", "--clerk-org-id", new_org, "--name", "Dev Co", database=True)
    assert result.returncode == 0, result.stderr
    assert (await _linked_tenant(app_engine, new_org)).name == "Dev Co"

    # An existing tenant, linked as the migrator; then again, unchanged.
    tenant_id = uuid4()
    created.tenant_ids.append(tenant_id)
    async with tenant_session(app_engine, tenant_id) as session:
        session.add(Tenant(id=tenant_id, name="Existing Co"))
    org_id = created.org()
    link = ("--clerk-org-id", org_id, "--tenant-id", str(tenant_id), "--operator", "ops")

    first = _run("dev", *link, database=True)
    assert first.returncode == 0, first.stderr
    tenant = await _linked_tenant(app_engine, org_id)
    assert tenant.id == tenant_id
    assert await _link_events(app_engine, tenant) == [
        {"source": "operator", "operator": "ops", "clerk_org_id": org_id, "created": False}
    ]
    again = _run("dev", *link, database=True)
    assert again.returncode == 0, again.stderr
    assert again.stdout.startswith("unchanged")

    # The organization is taken: another tenant is refused.
    taken = _run(
        "dev",
        "--clerk-org-id",
        org_id,
        "--tenant-id",
        str(uuid4()),
        "--operator",
        "ops",
        database=True,
    )
    assert taken.returncode == 1
    assert f"already linked to tenant {tenant_id}" in taken.stderr
