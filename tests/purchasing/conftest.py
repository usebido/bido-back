"""Fixtures for the purchase analysis tests (database ones skip without Postgres).

`purchasing_rows` deletes this feature's rows of both tenants before
`rfq_rows` deletes the RFQ-flow rows (suppliers, RFQs) and `people` the users.
"""

import io
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.db import tenant_session
from bido_back.db.models import (
    Analysis,
    AnalysisLine,
    BomLine,
    InventoryRow,
    Item,
    PoLine,
    ProductionPlanRow,
    PurchaseOrder,
    TemplateUpload,
)
from tests.approvals.conftest import People, people
from tests.db.conftest import app_engine, migrator_engine, two_tenants
from tests.rfqs.conftest import rfq_rows

__all__ = ["app_engine", "migrator_engine", "people", "rfq_rows", "two_tenants"]

CLEANUP = (
    AnalysisLine,
    Analysis,
    PoLine,
    PurchaseOrder,
    InventoryRow,
    BomLine,
    ProductionPlanRow,
    Item,
    TemplateUpload,
)


@pytest_asyncio.fixture
async def purchasing_rows(migrator_engine: AsyncEngine, rfq_rows: People) -> AsyncIterator[People]:
    yield rfq_rows
    for tenant_id in (rfq_rows.tenant, rfq_rows.other_tenant):
        async with tenant_session(migrator_engine, tenant_id) as session:
            for model in CLEANUP:
                await session.execute(delete(model).where(model.tenant_id == tenant_id))


class FakeStorage:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.blobs[key] = data

    async def get(self, key: str) -> bytes:
        return self.blobs[key]


def xlsx(rows: Sequence[Sequence[object]]) -> bytes:
    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]

    workbook = cast(Any, Workbook())
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
