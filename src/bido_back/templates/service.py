"""RFQ templates after onboarding: versions, variants per item category, defaults, resolution.

Every function takes the caller's `Principal` (or a tenant-scoped session)
and works under forced RLS.

* Versions: a template's current content lives on its row (`version` says
  which). Editing writes the new content as a new immutable row in
  `rfq_template_versions` (the first edit also snapshots the version it
  replaces) and moves the template to it. Old versions are never changed.
* Required fields: an edit may not drop a required sector field the
  previous version covered (`RequiredFieldsRemoved`, HTTP 422). Fields the
  template never covered are only suggested (`missing_required`).
* Variants: a copy of a template for one item category. At most one default
  per tenant and category; null is the general template.
* Resolution for an RFQ: the version recorded on the RFQ if any; otherwise
  the default variant for the lines' category when all lines share one, else
  the general default, else Bido's standard template (version 0, no id).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.catalog.contracts import LineCategory
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import Rfq, RfqLine, RfqTemplate, RfqTemplateVersion
from bido_back.onboarding.standard import STANDARD_TEMPLATE, TemplateContent
from bido_back.rfqs.errors import InvalidState, NotFound, RfqError
from bido_back.templates.required import (
    RequiredField,
    missing_required,
    removed_required,
    template_labels,
)
from bido_back.templates.schemas import (
    RequiredFieldView,
    TemplateDetail,
    TemplateEdit,
    TemplateSource,
    TemplateStatus,
    TemplateVersionView,
)

logger = logging.getLogger(__name__)

type Db = AsyncEngine | async_sessionmaker[AsyncSession]

FOOTER_SEPARATOR = " · "
STANDARD_VERSION = 0


class RequiredFieldsRemoved(RfqError):
    """An edit dropped required sector fields the previous version had."""

    def __init__(self, fields: Sequence[RequiredField]) -> None:
        self.keys = [f.key for f in fields]
        super().__init__(f"required fields cannot be removed: {', '.join(self.keys)}")


# --- Content -------------------------------------------------------------------------------


def footer_fields(footer: str | None) -> list[str]:
    if not footer:
        return []
    return [part.strip() for part in footer.split(FOOTER_SEPARATOR) if part.strip()]


def row_content(row: RfqTemplate) -> TemplateContent:
    return TemplateContent(
        language=row.language or STANDARD_TEMPLATE.language,
        greeting=row.greeting or "",
        columns=[str(c) for c in row.columns],
        footer_fields=footer_fields(row.footer),
        fields=[str(f) for f in row.fields],
    )


def _version_content(row: RfqTemplateVersion) -> TemplateContent:
    return TemplateContent(
        language=row.language,
        greeting=row.greeting,
        columns=[str(c) for c in row.columns],
        footer_fields=[str(f) for f in row.footer_fields],
        fields=[str(f) for f in row.fields],
    )


def required_views(content: TemplateContent) -> list[RequiredFieldView]:
    labels = template_labels(content.columns, content.footer_fields, content.fields)
    return [
        RequiredFieldView(
            key=f.key, kind=f.kind, label=f.label(content.language), conditional=f.conditional
        )
        for f in missing_required(labels)
    ]


def detail(row: RfqTemplate) -> TemplateDetail:
    content = row_content(row)
    return TemplateDetail(
        id=row.id,
        source=cast(TemplateSource, row.source),
        status=cast(TemplateStatus, row.status),
        category=cast(LineCategory | None, row.category),
        version=row.version,
        language=row.language,
        greeting=row.greeting,
        columns=content.columns,
        footer_fields=content.footer_fields,
        fields=content.fields,
        is_default=row.is_default,
        missing_required=required_views(content) if row.status == "ready" else [],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _snapshot(
    row: RfqTemplate, content: TemplateContent, user_id: UUID | None
) -> RfqTemplateVersion:
    return RfqTemplateVersion(
        tenant_id=row.tenant_id,
        template_id=row.id,
        version=row.version,
        language=content.language,
        greeting=content.greeting,
        columns=list(content.columns),
        footer_fields=list(content.footer_fields),
        fields=list(content.fields),
        created_by=user_id,
    )


# --- Reading -------------------------------------------------------------------------------


async def _get(session: AsyncSession, template_id: UUID, *, lock: bool = False) -> RfqTemplate:
    stmt = select(RfqTemplate).where(RfqTemplate.id == template_id)
    if lock:
        stmt = stmt.with_for_update()
    row = await session.scalar(stmt)
    if row is None:
        raise NotFound(f"template {template_id} not found")
    return row


async def _get_ready(session: AsyncSession, template_id: UUID) -> RfqTemplate:
    row = await _get(session, template_id, lock=True)
    if row.status != "ready":
        raise InvalidState(f"template {template_id} is {row.status}")
    return row


async def list_templates(db: Db, principal: Principal) -> list[TemplateDetail]:
    async with tenant_session(db, principal.tenant_id) as session:
        rows = await session.scalars(
            select(RfqTemplate).order_by(RfqTemplate.created_at.desc(), RfqTemplate.id)
        )
        return [detail(row) for row in rows]


async def get_template(db: Db, principal: Principal, template_id: UUID) -> TemplateDetail:
    async with tenant_session(db, principal.tenant_id) as session:
        return detail(await _get(session, template_id))


async def list_versions(
    db: Db, principal: Principal, template_id: UUID
) -> list[TemplateVersionView]:
    """Newest first. A template never edited has only its current version."""
    async with tenant_session(db, principal.tenant_id) as session:
        row = await _get(session, template_id)
        if row.status != "ready":
            return []
        versions = list(
            await session.scalars(
                select(RfqTemplateVersion)
                .where(RfqTemplateVersion.template_id == template_id)
                .order_by(RfqTemplateVersion.version.desc())
            )
        )
        if not versions:
            content = row_content(row)
            return [
                TemplateVersionView(
                    version=row.version,
                    current=True,
                    language=content.language,
                    greeting=content.greeting,
                    columns=content.columns,
                    footer_fields=content.footer_fields,
                    fields=content.fields,
                    created_at=row.created_at,
                )
            ]
        return [
            TemplateVersionView(
                version=v.version,
                current=v.version == row.version,
                language=v.language,
                greeting=v.greeting,
                columns=[str(c) for c in v.columns],
                footer_fields=[str(f) for f in v.footer_fields],
                fields=[str(f) for f in v.fields],
                created_at=v.created_at,
            )
            for v in versions
        ]


# --- Changing ------------------------------------------------------------------------------


async def edit_template(
    db: Db, principal: Principal, template_id: UUID, edit: TemplateEdit
) -> TemplateDetail:
    """Write `edit` as the template's next version; the old one stays as it was."""
    async with tenant_session(db, principal.tenant_id) as session:
        row = await _get_ready(session, template_id)
        before = row_content(row)
        after = TemplateContent(
            language=edit.language,
            greeting=edit.greeting,
            columns=list(edit.columns),
            footer_fields=list(edit.footer_fields),
            fields=list(edit.fields),
        )
        removed = removed_required(
            template_labels(before.columns, before.footer_fields, before.fields),
            template_labels(after.columns, after.footer_fields, after.fields),
        )
        if removed:
            raise RequiredFieldsRemoved(removed)

        has_history = await session.scalar(
            select(func.count())
            .select_from(RfqTemplateVersion)
            .where(RfqTemplateVersion.template_id == template_id)
        )
        if not has_history:
            session.add(_snapshot(row, before, row.created_by))
            await session.flush()
        row.version += 1
        row.language = after.language
        row.greeting = after.greeting
        row.columns = list(after.columns)
        row.footer = FOOTER_SEPARATOR.join(after.footer_fields)
        row.fields = list(after.fields)
        row.updated_at = datetime.now(UTC)
        session.add(_snapshot(row, after, principal.user_id))
        await session.flush()
        await session.refresh(row)
        logger.info(
            "template edited tenant=%s template=%s version=%d",
            principal.tenant_id,
            template_id,
            row.version,
        )
        return detail(row)


async def create_variant(
    db: Db, principal: Principal, template_id: UUID, category: LineCategory
) -> TemplateDetail:
    """A copy of the template's current version for one item category (not the default yet)."""
    async with tenant_session(db, principal.tenant_id) as session:
        source = await _get_ready(session, template_id)
        content = row_content(source)
        row = RfqTemplate(
            tenant_id=principal.tenant_id,
            created_by=principal.user_id,
            source=source.source,
            status="ready",
            language=content.language,
            greeting=content.greeting,
            columns=list(content.columns),
            footer=FOOTER_SEPARATOR.join(content.footer_fields),
            fields=list(content.fields),
            source_refs={"variant_of": str(source.id), "variant_of_version": source.version},
            category=category,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        logger.info(
            "template variant created tenant=%s template=%s from=%s",
            principal.tenant_id,
            row.id,
            template_id,
        )
        return detail(row)


async def make_default(db: Db, principal: Principal, template_id: UUID) -> TemplateDetail:
    """Make a ready template the default for its category (the previous one stops being it)."""
    async with tenant_session(db, principal.tenant_id) as session:
        row = await _get_ready(session, template_id)
        if not row.is_default:
            await session.execute(
                update(RfqTemplate)
                .where(
                    RfqTemplate.is_default,
                    RfqTemplate.id != template_id,
                    RfqTemplate.category.is_not_distinct_from(row.category),
                )
                .values(is_default=False, updated_at=func.now())
            )
            # The partial unique index must see the old default gone first.
            await session.flush()
            row.is_default = True
            row.updated_at = datetime.now(UTC)
            await session.flush()
        logger.info("template made default tenant=%s template=%s", principal.tenant_id, template_id)
        return detail(row)


# --- Resolution for an RFQ -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedTemplate:
    # None with version 0: Bido's standard template.
    template_id: UUID | None
    version: int
    content: TemplateContent


STANDARD = ResolvedTemplate(template_id=None, version=STANDARD_VERSION, content=STANDARD_TEMPLATE)


async def _content_at(
    session: AsyncSession, row: RfqTemplate, version: int
) -> TemplateContent | None:
    if version == row.version:
        return row_content(row)
    snapshot = await session.scalar(
        select(RfqTemplateVersion).where(
            RfqTemplateVersion.template_id == row.id, RfqTemplateVersion.version == version
        )
    )
    return _version_content(snapshot) if snapshot is not None else None


async def resolve_for_rfq(
    session: AsyncSession, rfq: Rfq, lines: Sequence[RfqLine]
) -> ResolvedTemplate:
    """The template version this RFQ's emails are rendered from (see the module docstring)."""
    if rfq.template_version == STANDARD_VERSION and rfq.template_id is None:
        return STANDARD
    if rfq.template_version is not None and rfq.template_id is not None:
        recorded = await session.get(RfqTemplate, rfq.template_id)
        if recorded is not None:
            content = await _content_at(session, recorded, rfq.template_version)
            if content is not None:
                return ResolvedTemplate(recorded.id, rfq.template_version, content)
    # Nothing recorded, or the recorded template is gone: pick as for a new RFQ.

    defaults = list(
        await session.scalars(
            select(RfqTemplate).where(RfqTemplate.is_default, RfqTemplate.status == "ready")
        )
    )
    by_category = {row.category: row for row in defaults}
    categories = {line.category for line in lines}
    chosen: RfqTemplate | None = None
    if len(categories) == 1:
        chosen = by_category.get(next(iter(categories)))
    if chosen is None:
        chosen = by_category.get(None)
    if chosen is None or not chosen.greeting:
        return STANDARD
    return ResolvedTemplate(chosen.id, chosen.version, row_content(chosen))


def record_template(rfq: Rfq, resolved: ResolvedTemplate) -> None:
    """Pin the RFQ to the version its first emails use; later sends reuse it."""
    gone = rfq.template_id is None and rfq.template_version not in (None, STANDARD_VERSION)
    if rfq.template_version is None or gone:
        rfq.template_id = resolved.template_id
        rfq.template_version = resolved.version
