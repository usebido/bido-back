"""The simulator tests reuse the RFQ flow fixtures (database, people, fakes).

`sandbox_rows` also deletes the simulator's rows (a sandbox email need not
belong to an invite) before `rfq_rows` deletes the RFQ-flow rows.
"""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.db import tenant_session
from bido_back.db.models import SandboxOutbound, SupplierPersona
from tests.approvals.conftest import People
from tests.rfqs.conftest import app_engine, migrator_engine, people, rfq_rows, two_tenants

__all__ = ["app_engine", "migrator_engine", "people", "rfq_rows", "sandbox_rows", "two_tenants"]


@pytest_asyncio.fixture
async def sandbox_rows(migrator_engine: AsyncEngine, rfq_rows: People) -> AsyncIterator[People]:
    yield rfq_rows
    for tenant_id in (rfq_rows.tenant, rfq_rows.other_tenant):
        async with tenant_session(migrator_engine, tenant_id) as session:
            for model in (SandboxOutbound, SupplierPersona):
                await session.execute(delete(model).where(model.tenant_id == tenant_id))
