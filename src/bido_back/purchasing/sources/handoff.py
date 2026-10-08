"""What a draft RFQ from a saved assessment starts with besides its lines.

- Default negotiation limits: each line's internal estimate (`AssessmentLine.price`,
  from reconciled received orders) gives the RFQ line its target and maximum unit
  price, converted to the RFQ's currency at the latest reference rate exactly like
  `purchasing.service.create_rfq_from_analysis`. Without an estimate or a rate the
  line has none. They are defaults: nothing is approved here; the buyer approves the
  limits in the negotiation mandate.
- The suppliers to ask, per line: the assessment's `suggested` ones (best-ranked
  first) or, for a line without suggestions (older snapshots), those with purchase
  history or an approved qualification. At most `per_line` per line, each supplier
  once. One the buyer's approved-source data disqualifies is never invited.
- Each is resolved to the tenant's OWN supplier records (the tenant session's row
  security: never another tenant's), by contact email (case-insensitive), else by
  exact normalized company name. A supplier the buyer does not have yet is created
  with its contact through the supplier service, only when the evidence gives both
  its company name and one valid email. Names and addresses are never invented, and
  `SupplierEvidence.name` is the company, never a contact person.
- Each resolved supplier becomes a DRAFT invite. Nothing is sent: the approval guard
  or the negotiation mandate, with the confirmed-domain rules, decides every email.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.auth import Principal
from bido_back.comparison.compute import Rate
from bido_back.db.models import Contact, Invite, Rfq, RfqLine, Supplier, SupplierContact
from bido_back.purchasing.analysis import PRICE_PLACES
from bido_back.purchasing.importers import norm
from bido_back.purchasing.schemas import InvitedSupplier
from bido_back.purchasing.sources.contracts import AssessmentLine, PriceBenchmark, SupplierEvidence
from bido_back.rfqs import suppliers as supplier_service
from bido_back.rfqs.currency import fx_factor
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest, EmailAddress
from bido_back.rfqs.service import add_draft_invite

# rfq_lines store limits as NUMERIC(18, 6).
LIMIT_CEILING: Final = Decimal(10) ** 12
# Qualification statuses of the buyer's approved-source data, compared after `norm`.
APPROVED: Final = frozenset(
    {"homologado", "homologada", "aprovado", "aprovada", "approved", "qualificado",
     "qualificada", "qualified", "certificado", "certificada", "certified", "preferencial",
     "preferred", "ativo", "ativa", "active"}
)  # fmt: skip
DISQUALIFIED: Final = frozenset(
    {"bloqueado", "bloqueada", "blocked", "suspenso", "suspensa", "suspended", "reprovado",
     "reprovada", "rejected", "desqualificado", "desqualificada", "disqualified",
     "descredenciado", "descredenciada", "inativo", "inativa", "inactive", "vencido",
     "vencida", "expired", "cancelado", "cancelada", "cancelled", "canceled", "debarred"}
)  # fmt: skip

SkipReason = Literal[
    "no_contact_email", "invalid_email", "no_company_name", "ambiguous_identity", "disqualified"
]
_EMAIL: Final[TypeAdapter[str]] = TypeAdapter(EmailAddress)


class AssessmentInvitedSupplier(InvitedSupplier):
    """A supplier on the draft RFQ as a DRAFT invite; nothing was sent to it."""

    # The address of the contact the draft invite uses.
    email: str | None = None
    # Created now, with its contact, from the assessment's company name and email.
    supplier_created: bool = False
    # The assessment's email was added now as a contact of the buyer's existing supplier.
    contact_created: bool = False
    # The assessed parts it was chosen for (empty: invited some other way).
    part_numbers: list[str] = Field(default_factory=list[str])


class AssessmentSkippedSupplier(BaseModel):
    """An assessed supplier that was not invited."""

    # The buyer's supplier, when exactly one was found.
    supplier_id: UUID | None = None
    name: str | None = None
    # As the evidence gives it (also when it is not a valid address).
    email: str | None = None
    supplier_code: str | None = None
    reason: SkipReason = Field(
        description=(
            "no_contact_email: no address in the evidence nor on the buyer's supplier; "
            "invalid_email: the evidence's email is not one valid address; "
            "no_company_name: an address no supplier of the buyer has, without a company "
            "name to create one; ambiguous_identity: the email or name matches several of "
            "the buyer's suppliers; disqualified: the buyer's approved-source data blocks it"
        )
    )
    part_numbers: list[str] = Field(default_factory=list[str])


@dataclass
class WantedSupplier:
    """An assessed supplier to invite, once per identity, with the parts it is wanted for."""

    evidence: SupplierEvidence
    part_numbers: list[str] = field(default_factory=list[str])
    disqualified: bool = False


@dataclass(frozen=True, slots=True)
class _Resolved:
    supplier_id: UUID
    name: str
    contact_id: UUID
    email: str | None
    supplier_created: bool = False
    contact_created: bool = False


def default_limits(
    price: PriceBenchmark | None, currency: str, rates: Mapping[str, Rate]
) -> tuple[Decimal, Decimal] | None:
    """The estimate's (target, maximum) unit prices in the RFQ's `currency`; None without
    a usable estimate or an exchange rate."""
    if price is None:
        return None
    factor = fx_factor(price.currency.strip().upper(), currency, rates)
    if factor is None:
        return None
    target = (price.target_price * factor).quantize(PRICE_PLACES)
    maximum = (price.max_price * factor).quantize(PRICE_PLACES)
    if not 0 < target <= maximum < LIMIT_CEILING:
        return None
    return target, maximum


def wanted_suppliers(lines: Sequence[AssessmentLine], per_line: int) -> list[WantedSupplier]:
    """The suppliers to invite for these RFQ lines, in line and rank order (no I/O)."""
    wanted: dict[object, WantedSupplier] = {}
    blocked: dict[object, WantedSupplier] = {}
    for line in lines:
        candidates = [s for s in line.suppliers if s.suggested] or [
            s for s in line.suppliers if _has_history(s) or _status(s) in APPROVED
        ]
        for supplier in candidates:
            if _status(supplier) in DISQUALIFIED:
                _want(blocked, supplier, line.part_number).disqualified = True
        eligible = [s for s in candidates if _status(s) not in DISQUALIFIED]
        for supplier in eligible[:per_line]:
            _want(wanted, supplier, line.part_number)
    return [*wanted.values(), *blocked.values()]


async def invite_suppliers(
    session: AsyncSession, principal: Principal, rfq: Rfq, wanted: Sequence[WantedSupplier]
) -> tuple[list[AssessmentInvitedSupplier], list[AssessmentSkippedSupplier]]:
    """Invite the wanted suppliers to the new draft `rfq` as DRAFT invites, in the caller's
    transaction, creating the ones the evidence fully identifies and the buyer lacks."""
    # Resolving then creating is serialized per tenant, so concurrent handoffs cannot
    # both create the same supplier.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"assessment_suppliers:{principal.tenant_id}"},
    )
    invited: dict[UUID, AssessmentInvitedSupplier] = {}
    skipped: list[AssessmentSkippedSupplier] = []
    for entry in wanted:
        outcome = await _resolve(session, principal.tenant_id, entry, create=True)
        if isinstance(outcome, AssessmentSkippedSupplier):
            skipped.append(outcome)
        elif outcome is not None:
            done = invited.get(outcome.supplier_id)
            if done is None:
                await add_draft_invite(
                    session, principal, rfq, outcome.supplier_id, outcome.contact_id
                )
                invited[outcome.supplier_id] = AssessmentInvitedSupplier(
                    supplier_id=outcome.supplier_id,
                    name=outcome.name,
                    email=outcome.email,
                    supplier_created=outcome.supplier_created,
                    contact_created=outcome.contact_created,
                    part_numbers=list(entry.part_numbers),
                )
            else:
                _merge_parts(done.part_numbers, entry.part_numbers)
    return list(invited.values()), _not_invited(skipped, invited)


async def invited_suppliers(
    session: AsyncSession, principal: Principal, rfq: Rfq, wanted: Sequence[WantedSupplier]
) -> tuple[list[AssessmentInvitedSupplier], list[AssessmentSkippedSupplier]]:
    """The same answer for an existing draft, read-only: its invites now, and the wanted
    suppliers that cannot be invited, with why."""
    rows = await session.execute(
        select(Invite.supplier_id, Supplier.name, Contact.email)
        .join(Supplier, Supplier.id == Invite.supplier_id)
        .join(Contact, Contact.id == Invite.contact_id)
        .where(Invite.rfq_id == rfq.id)
        .order_by(Invite.created_at, Invite.id)
    )
    on_rfq = {
        supplier_id: AssessmentInvitedSupplier(supplier_id=supplier_id, name=name, email=email)
        for supplier_id, name, email in rows
    }
    # In the order the first call invited them (one transaction: equal created_at), then
    # any the buyer added some other way.
    invited: dict[UUID, AssessmentInvitedSupplier] = {}
    skipped: list[AssessmentSkippedSupplier] = []
    for entry in wanted:
        outcome = await _resolve(session, principal.tenant_id, entry, create=False)
        if isinstance(outcome, AssessmentSkippedSupplier):
            skipped.append(outcome)
        elif outcome is not None and outcome.supplier_id in on_rfq:
            found = invited.setdefault(outcome.supplier_id, on_rfq[outcome.supplier_id])
            _merge_parts(found.part_numbers, entry.part_numbers)
    invited |= {key: value for key, value in on_rfq.items() if key not in invited}
    return list(invited.values()), _not_invited(skipped, invited)


async def lines_without_limits(session: AsyncSession, rfq_id: UUID) -> list[int]:
    """Positions of the RFQ's lines missing a target or a maximum unit price."""
    return list(
        await session.scalars(
            select(RfqLine.position)
            .where(
                RfqLine.rfq_id == rfq_id,
                or_(RfqLine.target_unit_price.is_(None), RfqLine.max_unit_price.is_(None)),
            )
            .order_by(RfqLine.position)
        )
    )


def _status(supplier: SupplierEvidence) -> str:
    return norm(supplier.qualification_status or "")


def _has_history(supplier: SupplierEvidence) -> bool:
    return (
        supplier.orders > 0
        or supplier.last_price is not None
        or supplier.ordered_at is not None
        or supplier.last_received_at is not None
    )


def _email(raw: str | None) -> str | None:
    """One valid address, stripped; None when absent or not one address."""
    if raw is None or not raw.strip():
        return None
    try:
        return _EMAIL.validate_python(raw)
    except ValidationError:
        return None


def _company(raw: str | None) -> str | None:
    """The company name with its whitespace collapsed; None when blank."""
    if raw is None:
        return None
    return " ".join(raw.split()) or None


def _identity(supplier: SupplierEvidence) -> tuple[str | None, str | None, str | None] | None:
    email, name = _email(supplier.email), _company(supplier.name)
    if email is None and name is None:
        return (None, None, supplier.supplier_code) if supplier.supplier_code else None
    return (email.lower() if email else None, name.lower() if name else None, None)


def _want(
    wanted: dict[object, WantedSupplier], supplier: SupplierEvidence, part_number: str
) -> WantedSupplier:
    # Evidence without any identity is its own entry: it cannot be the same as another.
    entry = wanted.setdefault(_identity(supplier) or object(), WantedSupplier(supplier))
    _merge_parts(entry.part_numbers, [part_number])
    return entry


def _merge_parts(into: list[str], parts: Sequence[str]) -> None:
    into.extend(part for part in parts if part not in into)


def _not_invited(
    skipped: list[AssessmentSkippedSupplier], invited: Mapping[UUID, AssessmentInvitedSupplier]
) -> list[AssessmentSkippedSupplier]:
    """Skips of suppliers that another entry of the same supplier did invite are not news."""
    return [s for s in skipped if s.supplier_id is None or s.supplier_id not in invited]


def _skip(
    entry: WantedSupplier,
    reason: SkipReason,
    *,
    supplier_id: UUID | None = None,
    name: str | None = None,
) -> AssessmentSkippedSupplier:
    evidence = entry.evidence
    return AssessmentSkippedSupplier(
        supplier_id=supplier_id,
        name=name or _company(evidence.name),
        email=evidence.email.strip() if evidence.email and evidence.email.strip() else None,
        supplier_code=evidence.supplier_code,
        reason=reason,
        part_numbers=list(entry.part_numbers),
    )


async def _resolve(
    session: AsyncSession, tenant_id: UUID, entry: WantedSupplier, *, create: bool
) -> _Resolved | AssessmentSkippedSupplier | None:
    """The buyer's supplier and contact for `entry`, creating them when `create` and the
    evidence identifies a new one; a skip when it cannot be invited. None only when
    `create` is False and it would have been created."""
    if entry.disqualified:
        return _skip(entry, "disqualified")
    evidence = entry.evidence
    email, name = _email(evidence.email), _company(evidence.name)
    no_email: SkipReason = (
        "invalid_email" if evidence.email and evidence.email.strip() else "no_contact_email"
    )
    if email is not None:
        rows = (
            await session.execute(
                select(Supplier.id, Supplier.name, Contact.id, Contact.email)
                .join(SupplierContact, SupplierContact.supplier_id == Supplier.id)
                .join(Contact, Contact.id == SupplierContact.contact_id)
                .where(
                    func.lower(Contact.email) == email.lower(),
                    Contact.pseudonymized_at.is_(None),
                )
                .order_by(Contact.created_at, Contact.id)
            )
        ).all()
        if len({row[0] for row in rows}) > 1:
            return _skip(entry, "ambiguous_identity")
        if rows:
            supplier_id, supplier_name, contact_id, address = rows[0]
            return _Resolved(supplier_id, supplier_name, contact_id, address)
    if name is not None:
        named = (
            await session.execute(
                select(Supplier.id, Supplier.name)
                .where(
                    func.lower(func.btrim(func.regexp_replace(Supplier.name, r"\s+", " ", "g")))
                    == name.lower()
                )
                .order_by(Supplier.created_at, Supplier.id)
                .limit(2)
            )
        ).all()
        if len(named) > 1:
            return _skip(entry, "ambiguous_identity")
        if named:
            supplier_id, supplier_name = named[0]
            contact = (
                await session.execute(
                    select(Contact.id, Contact.email)
                    .join(SupplierContact, SupplierContact.contact_id == Contact.id)
                    .where(
                        SupplierContact.supplier_id == supplier_id,
                        Contact.email.is_not(None),
                        Contact.email != "",
                        Contact.pseudonymized_at.is_(None),
                    )
                    .order_by(Contact.created_at, Contact.id)
                    .limit(1)
                )
            ).one_or_none()
            if contact is not None:
                return _Resolved(supplier_id, supplier_name, contact[0], contact[1])
            if email is None:
                return _skip(entry, no_email, supplier_id=supplier_id, name=supplier_name)
            if not create:
                return None
            added = await supplier_service.add_contact_in(
                session, tenant_id, supplier_id, ContactIn(email=email)
            )
            return _Resolved(
                supplier_id, supplier_name, added.id, added.email, contact_created=True
            )
    if email is None:
        return _skip(entry, no_email)
    if name is None:
        return _skip(entry, "no_company_name")
    try:
        request = CreateSupplierRequest(name=name, contacts=[ContactIn(email=email)])
    except ValidationError:
        return _skip(entry, "no_company_name")
    if not create:
        return None
    created = await supplier_service.create_supplier_in(session, tenant_id, request)
    contact = created.contacts[0]
    return _Resolved(created.id, created.name, contact.id, contact.email, supplier_created=True)
