"""Database fixtures for the compliance tests (skipped without Postgres, failing in CI).

`compliance_rows` cleans this feature's rows of both tenants before
`rfq_rows` deletes the RFQ-flow rows and `people` the users.
"""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.db import tenant_session
from bido_back.db.models import ComplianceRequirement, SupplierComplianceDocument
from tests.approvals.conftest import People, people
from tests.db.conftest import app_engine, migrator_engine, two_tenants
from tests.rfqs.conftest import rfq_rows

__all__ = ["app_engine", "migrator_engine", "people", "rfq_rows", "two_tenants"]


@pytest_asyncio.fixture
async def compliance_rows(migrator_engine: AsyncEngine, rfq_rows: People) -> AsyncIterator[People]:
    yield rfq_rows
    for tenant_id in (rfq_rows.tenant, rfq_rows.other_tenant):
        async with tenant_session(migrator_engine, tenant_id) as session:
            for model in (SupplierComplianceDocument, ComplianceRequirement):
                await session.execute(delete(model).where(model.tenant_id == tenant_id))
