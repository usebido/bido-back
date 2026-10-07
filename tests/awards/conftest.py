"""The award tests reuse the RFQ flow fixtures and clean the award rows first."""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.db import tenant_session
from bido_back.db.models import Award, AwardNotice, IssuedPurchaseOrder, IssuedPurchaseOrderLine
from tests.approvals.conftest import People
from tests.rfqs.conftest import app_engine, migrator_engine, people, rfq_rows, two_tenants

__all__ = ["app_engine", "migrator_engine", "people", "rfq_rows", "two_tenants"]


@pytest_asyncio.fixture
async def award_rows(migrator_engine: AsyncEngine, rfq_rows: People) -> AsyncIterator[People]:
    yield rfq_rows
    for tenant_id in (rfq_rows.tenant, rfq_rows.other_tenant):
        async with tenant_session(migrator_engine, tenant_id) as session:
            for model in (AwardNotice, IssuedPurchaseOrderLine, IssuedPurchaseOrder, Award):
                await session.execute(delete(model).where(model.tenant_id == tenant_id))
