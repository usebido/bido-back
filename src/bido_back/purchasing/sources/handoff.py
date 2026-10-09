"""What a draft RFQ from a saved assessment starts with besides its lines.

- Default negotiation limits: each line's internal estimate (`AssessmentLine.price`,
  from reconciled received orders) gives the RFQ line its target and maximum unit
  price, converted to the RFQ's currency at the latest reference rate exactly like
  `purchasing.service.create_rfq_from_analysis`. Without an estimate (or one from a
  single purchase) or a rate the line has none; nothing else, such as a supplier's last
  price, ever sets them. They are proposals shown with their basis: nothing is approved
  here; the buyer approves the limits in the negotiation mandate.
- The suppliers to ask, per line: the assessment's `suggested` ones (best-ranked
  first) or, for a line without suggestions (older snapshots), those with purchase
  history or an approved qualification. At most `per_line` per line, each supplier
  once. One the buyer's approved-source data disqualifies is never invited.
- Each is resolved to the tenant's OWN supplier records (the tenant session's row
  security: never another tenant's), by contact email (case-insensitive), else by
  exact normalized company name. A supplier the buyer does not have yet is created
  with its contact through the supplier service, only when the evidence gives both
  its company name and one valid email. Names and addresses are never invented, and
  `SupplierEvidence.name` is the company, never a contact person. A new supplier is
  written to in its country's language when the buyer's files give its country
  (`SupplierEvidence.country`), else in the language of the buyer's default RFQ
  template, else in English; an existing supplier keeps its own setting.
- Each resolved supplier becomes a DRAFT invite. Nothing is sent: the approval guard
  or the negotiation mandate, with the confirmed-domain rules, decides every email.
- Each RFQ line records which tenant supplier each of its assessment suppliers became
  (`supplier_links`), so the comparison can use the assessment's measured history.

`handoff` does all of it for a new draft, or only works it out (writing nothing) for
the preview the buyer confirms first: one path, so the preview is what runs.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.auth import Principal
from bido_back.comparison.compute import Rate
from bido_back.comparison.loader import load_rates
from bido_back.db.models import (
    Contact,
    Invite,
    Rfq,
    RfqLine,
    RfqTemplate,
    Supplier,
    SupplierContact,
)
from bido_back.purchasing.analysis import PRICE_PLACES
from bido_back.purchasing.schemas import InvitedSupplier
from bido_back.purchasing.sources.contracts import AssessmentLine, PriceBenchmark, SupplierEvidence
from bido_back.purchasing.sources.status import APPROVED, disqualified, status
from bido_back.purchasing.sources.timing import late
from bido_back.rfqs import suppliers as supplier_service
from bido_back.rfqs.currency import fx_factor
from bido_back.rfqs.language import DEFAULT_LANGUAGE, country_language
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest, EmailAddress
from bido_back.rfqs.service import add_draft_invite

# rfq_lines store limits as NUMERIC(18, 6).
LIMIT_CEILING: Final = Decimal(10) ** 12
# A benchmark of one purchase is a single last price, not an estimate: no limits from it.
MIN_RECEIVED_ORDERS: Final = 2

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


class LimitsBasis(BaseModel):
    """Why a line's default limits are what they are: Bido's internal estimate from the
    buyer's completed purchases, a proposal until the buyer approves the mandate."""

    rule: str
    received_orders: int
    window_days: int
    last_ordered_at: date | None = None
    last_price: Decimal
    avg_price: Decimal
    # The currency of these prices; limits in another RFQ currency were converted at the
    # latest reference rate.
    currency: str


class PlannedInvite(BaseModel):
    """A supplier a new draft gets as a DRAFT invite, before the draft exists."""

    # None: the supplier does not exist yet and would be created from the evidence.
    supplier_id: UUID | None = None
    name: str
    email: str | None = None
    supplier_created: bool = False
    contact_created: bool = False
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


@dataclass
class Handoff:
    """A new draft's default limits and suppliers, per line in order: done, or planned."""

    limits: list[tuple[Decimal, Decimal] | None]
    invited: list[PlannedInvite]
    skipped_suppliers: list[AssessmentSkippedSupplier]
    # Per line: each assessment supplier of the line and the invited supplier it became.
    supplier_links: list[list[dict[str, JsonValue]]]

    @property
    def lines_without_limits(self) -> list[int]:
        return [position for position, found in enumerate(self.limits, start=1) if found is None]

    def done(self) -> list[AssessmentInvitedSupplier]:
        """The invites of a handoff that ran (every supplier exists)."""
        if any(planned.supplier_id is None for planned in self.invited):
            raise RuntimeError("a planned supplier is not an invite")
        return [AssessmentInvitedSupplier.model_validate(p.model_dump()) for p in self.invited]


@dataclass(frozen=True, slots=True)
class _Resolved:
    """The buyer's supplier and contact for a wanted supplier. While planning, the ids of
    a supplier or contact that would be created are None."""

    supplier_id: UUID | None
    name: str
    contact_id: UUID | None
    email: str | None
    supplier_created: bool = False
    contact_created: bool = False


@dataclass
class _Created:
    """What this handoff created (or, planning, would create), for the wanted suppliers
    after it: the database finds them once they exist, a plan only here."""

    by_email: dict[str, _Resolved] = field(default_factory=dict[str, _Resolved])
    by_name: dict[str, _Resolved] = field(default_factory=dict[str, _Resolved])
    by_supplier: dict[UUID, _Resolved] = field(default_factory=dict[UUID, _Resolved])

    def add(self, resolved: _Resolved, email: str, name: str | None) -> _Resolved:
        self.by_email[email.lower()] = resolved
        if name is not None:
            self.by_name[name.lower()] = resolved
        if resolved.supplier_id is not None:
            self.by_supplier[resolved.supplier_id] = resolved
        return resolved


def default_limits(
    price: PriceBenchmark | None, currency: str, rates: Mapping[str, Rate]
) -> tuple[Decimal, Decimal] | None:
    """The estimate's (target, maximum) unit prices in the RFQ's `currency`; None without
    a usable estimate (none, or from fewer than `MIN_RECEIVED_ORDERS` received orders) or
    an exchange rate. Never derived from anything but the estimate."""
    if price is None or price.observations < MIN_RECEIVED_ORDERS:
        return None
    factor = fx_factor(price.currency.strip().upper(), currency, rates)
    if factor is None:
        return None
    target = (price.target_price * factor).quantize(PRICE_PLACES)
    maximum = (price.max_price * factor).quantize(PRICE_PLACES)
    if not 0 < target <= maximum < LIMIT_CEILING:
        return None
    return target, maximum


def limits_basis(price: PriceBenchmark) -> LimitsBasis:
    """The basis of limits from `price`, for the buyer to see why (`default_limits`)."""
    top = max(price.last_price, price.avg_price)
    margin = ((price.max_price / top - 1) * 100).quantize(Decimal("0.1")) if top > 0 else None
    return LimitsBasis(
        rule=(
            "target = the lower of the last and the average price paid; maximum = the "
            "higher of the two"
            + (f" plus {margin:f}%" if margin is not None else "")
            + f", over the completed purchases of the last {price.window_days} days"
        ),
        received_orders=price.observations,
        window_days=price.window_days,
        last_ordered_at=price.last_ordered_at,
        last_price=price.last_price,
        avg_price=price.avg_price,
        currency=price.currency,
    )


def wanted_suppliers(lines: Sequence[AssessmentLine], per_line: int) -> list[WantedSupplier]:
    """The suppliers to invite for these RFQ lines, in line and rank order (no I/O)."""
    wanted: dict[object, WantedSupplier] = {}
    blocked: dict[object, WantedSupplier] = {}
    for line in lines:
        # A source the timing check found too late is left out (the line's assumptions say
        # so); the buyer can still invite it.
        timely = [s for s in line.suppliers if not late(s)]
        suggested = [s for s in timely if s.suggested]
        candidates = suggested or [s for s in timely if _has_history(s) or status(s) in APPROVED]
        # Approved sources of supply with a current agreement compete too.
        chosen = {id(s) for s in candidates}
        candidates += [
            s
            for s in timely
            if id(s) not in chosen and status(s) in APPROVED and s.contract_expired is False
        ]
        for supplier in candidates:
            if disqualified(supplier):
                _want(blocked, supplier, line.part_number).disqualified = True
        eligible = [s for s in candidates if not disqualified(s)]
        for supplier in eligible[:per_line]:
            _want(wanted, supplier, line.part_number)
    return [*wanted.values(), *blocked.values()]


async def handoff(
    session: AsyncSession,
    principal: Principal,
    lines: Sequence[AssessmentLine],
    *,
    currency: str,
    per_line: int,
    rfq: Rfq | None,
) -> Handoff:
    """Default limits and suppliers for a new draft with `lines`, in the caller's tenant
    session. With `rfq` (that new draft) it invites them as DRAFT invites, creating the
    suppliers the evidence fully identifies; without, it writes nothing and says what it
    would do. The caller sets the limits and links on the RFQ lines."""
    rates = await load_rates(session, datetime.now(UTC).date())
    limits = [default_limits(line.price, currency, rates) for line in lines]
    if rfq is not None:
        # Resolving then creating is serialized per tenant, so concurrent handoffs
        # cannot both create the same supplier.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"assessment_suppliers:{principal.tenant_id}"},
        )
    created = _Created()
    invited: dict[object, PlannedInvite] = {}
    skipped: list[AssessmentSkippedSupplier] = []
    linked: dict[tuple[str | None, str | None, str | None], UUID] = {}
    buyer_language = await _buyer_language(session) if rfq is not None else None
    for entry in wanted_suppliers(lines, per_line):
        outcome = await _resolve(
            session,
            principal.tenant_id,
            entry,
            created,
            create=rfq is not None,
            buyer_language=buyer_language,
        )
        if isinstance(outcome, AssessmentSkippedSupplier):
            skipped.append(outcome)
            continue
        # A supplier still to be created has no id: the plan holds it by itself.
        key: object = outcome.supplier_id if outcome.supplier_id is not None else outcome
        planned = invited.get(key)
        if planned is not None:
            _merge_parts(planned.part_numbers, entry.part_numbers)
        else:
            if rfq is not None:
                if outcome.supplier_id is None or outcome.contact_id is None:
                    raise RuntimeError("an applied handoff resolves every supplier and contact")
                await add_draft_invite(
                    session, principal, rfq, outcome.supplier_id, outcome.contact_id
                )
            invited[key] = PlannedInvite(
                supplier_id=outcome.supplier_id,
                name=outcome.name,
                email=outcome.email,
                supplier_created=outcome.supplier_created,
                contact_created=outcome.contact_created,
                part_numbers=list(entry.part_numbers),
            )
        identity = _identity(entry.evidence)
        if identity is not None and outcome.supplier_id is not None:
            linked[identity] = outcome.supplier_id
    on_draft = {s.supplier_id for s in invited.values() if s.supplier_id is not None}
    return Handoff(
        limits=limits,
        invited=list(invited.values()),
        skipped_suppliers=_not_invited(skipped, on_draft),
        supplier_links=[_links(line, linked) for line in lines],
    )


async def invited_suppliers(
    session: AsyncSession, principal: Principal, rfq: Rfq, wanted: Sequence[WantedSupplier]
) -> tuple[list[AssessmentInvitedSupplier], list[AssessmentSkippedSupplier]]:
    """For an existing draft, read-only: its invites now, in the order the handoff made
    them, and the wanted suppliers that cannot be invited, with why."""
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
    # One transaction made them (equal created_at): the handoff's order, then any the
    # buyer added some other way.
    invited: dict[UUID, AssessmentInvitedSupplier] = {}
    skipped: list[AssessmentSkippedSupplier] = []
    created = _Created()
    for entry in wanted:
        outcome = await _resolve(session, principal.tenant_id, entry, created, create=False)
        if isinstance(outcome, AssessmentSkippedSupplier):
            skipped.append(outcome)
        elif outcome.supplier_id is not None and outcome.supplier_id in on_rfq:
            found = invited.setdefault(outcome.supplier_id, on_rfq[outcome.supplier_id])
            _merge_parts(found.part_numbers, entry.part_numbers)
    invited |= {key: value for key, value in on_rfq.items() if key not in invited}
    return list(invited.values()), _not_invited(skipped, set(invited))


async def rfq_line_limits(
    session: AsyncSession, rfq_id: UUID
) -> dict[int, tuple[Decimal, Decimal] | None]:
    """An RFQ's (target, maximum) by line position; None when either is missing."""
    rows = await session.execute(
        select(RfqLine.position, RfqLine.target_unit_price, RfqLine.max_unit_price)
        .where(RfqLine.rfq_id == rfq_id)
        .order_by(RfqLine.position)
    )
    return {
        position: (target, maximum) if target is not None and maximum is not None else None
        for position, target, maximum in rows
    }


async def _buyer_language(session: AsyncSession) -> str | None:
    """The language of the buyer's own general default RFQ template, if it has one."""
    language = await session.scalar(
        select(RfqTemplate.language)
        .where(
            RfqTemplate.is_default,
            RfqTemplate.status == "ready",
            RfqTemplate.category.is_(None),
        )
        .limit(1)
    )
    code = (language or "").strip().lower()
    return code if len(code) == 2 and code.isalpha() else None


def new_supplier_language(evidence: SupplierEvidence, buyer_language: str | None) -> str:
    """A new supplier's email language: its country's, else the buyer's, else English."""
    return country_language(evidence.country) or buyer_language or DEFAULT_LANGUAGE


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
    skipped: list[AssessmentSkippedSupplier], invited: set[UUID]
) -> list[AssessmentSkippedSupplier]:
    """Skips of suppliers that another entry of the same supplier did invite are not news."""
    return [s for s in skipped if s.supplier_id is None or s.supplier_id not in invited]


def _links(
    line: AssessmentLine, linked: Mapping[tuple[str | None, str | None, str | None], UUID]
) -> list[dict[str, JsonValue]]:
    """Each assessment supplier of the line that is an invited supplier, as the assessment
    names it (the comparison matches its measured history by code, else by name)."""
    links: list[dict[str, JsonValue]] = []
    for evidence in line.suppliers:
        identity = _identity(evidence)
        supplier_id = linked.get(identity) if identity is not None else None
        if supplier_id is not None:
            links.append(
                {
                    "supplier_id": str(supplier_id),
                    "supplier_code": evidence.supplier_code,
                    "name": evidence.name,
                    "email": evidence.email,
                }
            )
    return links


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


async def _resolve(  # noqa: C901 - one decision list, in the order it is applied
    session: AsyncSession,
    tenant_id: UUID,
    entry: WantedSupplier,
    created: _Created,
    *,
    create: bool,
    buyer_language: str | None = None,
) -> _Resolved | AssessmentSkippedSupplier:
    """The buyer's supplier and contact for `entry`, or why it cannot be invited. A new
    supplier or contact is created only when `create`; otherwise it is only planned."""
    if entry.disqualified:
        return _skip(entry, "disqualified")
    evidence = entry.evidence
    email, name = _email(evidence.email), _company(evidence.name)
    no_email: SkipReason = (
        "invalid_email" if evidence.email and evidence.email.strip() else "no_contact_email"
    )
    if email is not None:
        if (done := created.by_email.get(email.lower())) is not None:
            return done
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
        if (done := created.by_name.get(name.lower())) is not None:
            return done
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
            if (done := created.by_supplier.get(supplier_id)) is not None:
                return done
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
            if create:
                added = await supplier_service.add_contact_in(
                    session, tenant_id, supplier_id, ContactIn(email=email)
                )
                resolved = _Resolved(
                    supplier_id, supplier_name, added.id, added.email, contact_created=True
                )
            else:
                resolved = _Resolved(supplier_id, supplier_name, None, email, contact_created=True)
            return created.add(resolved, email, None)
    if email is None:
        return _skip(entry, no_email)
    if name is None:
        return _skip(entry, "no_company_name")
    try:
        request = CreateSupplierRequest(
            name=name,
            country=evidence.country,
            language=new_supplier_language(evidence, buyer_language),
            contacts=[ContactIn(email=email)],
        )
    except ValidationError:
        return _skip(entry, "no_company_name")
    if create:
        view = await supplier_service.create_supplier_in(session, tenant_id, request)
        contact_view = view.contacts[0]
        resolved = _Resolved(
            view.id, view.name, contact_view.id, contact_view.email, supplier_created=True
        )
    else:
        resolved = _Resolved(None, name, None, email, supplier_created=True)
    return created.add(resolved, email, name)
