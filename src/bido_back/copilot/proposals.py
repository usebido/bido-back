"""The buyer's go-ahead before the Copilot creates an RFQ or invites suppliers.

Those actions send nothing, but they change the buyer's data, so the Copilot
never takes them on its own initiative. A gated tool works in two calls:

1. Without `proposal_id`: it checks the arguments, works out exactly what it
   would do (the preview: lines with quantities and units, suppliers with the
   email that would be used...), stores both in `copilot_proposals` with this
   conversation and run, and does nothing else. The Copilot shows the preview
   and asks.
2. With `proposal_id`, from a LATER run of the same conversation (the buyer
   answered in their own message): it runs the STORED arguments, whatever the
   model passes now, so what happens is what the buyer saw. A proposal is used
   once and expires after `PROPOSAL_TTL`.

Claiming marks the row used in the same statement that checks it, so two
confirmations racing cannot both run; a run that then fails releases it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import CopilotProposal
from bido_back.purchasing.sources.handoff import AssessmentSkippedSupplier, LimitsBasis

type Db = AsyncEngine | async_sessionmaker[AsyncSession]

PROPOSAL_TTL: Final = timedelta(hours=24)

PROPOSAL_ARG: Final = (
    "Leave empty the first time: nothing is done, and the answer is a preview to show the "
    "buyer with its proposal_id. Pass that proposal_id only after the buyer confirmed it in "
    "their own later message; the stored proposal is what runs"
)


# --- What the buyer is shown ------------------------------------------------------------


class ProposedLine(BaseModel):
    position: int
    description: str
    # The buyer's own code: show it as the line's reference. `mpn` is the manufacturer's.
    part_number: str | None = None
    mpn: str | None = None
    quantities: list[Decimal]
    unit: str
    need_by: date | None = None
    # From an assessment: the recommended source's estimated arrival, set only when it
    # lands after `need_by`. Say so and ask whether to keep the date or move it to this
    # one; the negotiation's lead-time limit follows the line's need-by.
    arrives_after_need_by: date | None = None
    incoterm: str | None = None
    delivery_location: str | None = None
    # The negotiation's default limits, in the RFQ's currency (from an assessment: its
    # internal estimate); null: none. Proposals: the buyer approves limits in the mandate.
    target_unit_price: Decimal | None = None
    max_unit_price: Decimal | None = None
    # Why those limits: the rule, how many received orders, the window, the latest order.
    limits_basis: LimitsBasis | None = None
    # From an assessment: how reliable the quantity is and what it rests on. Show them
    # with the line; they do not stop the draft.
    confidence: Literal["high", "medium", "low"] | None = None
    assumptions: list[str] = Field(default_factory=list[str])


class ProposedSupplier(BaseModel):
    name: str
    email: str | None = Field(
        description="The contact the invite would use; null = no email, it would be skipped"
    )
    # The lines (part numbers) it would be invited for, when the action chooses by line.
    part_numbers: list[str] = Field(default_factory=list[str])
    # Bido would first create the supplier with this contact, from the files' company name
    # and email; or add this email as a contact of the buyer's existing supplier.
    new_supplier: bool = False
    new_contact: bool = False
    # The language of the RFQ document it would receive (its own, else its country's,
    # else English), so the buyer sees "Cobre Sul: pt" before confirming. None: not shown.
    document_language: str | None = Field(
        default=None,
        description="The language of the RFQ document this supplier would receive; show it",
    )


class QuotedLinePreview(BaseModel):
    """One line of a supplier's quote as read from the buyer's file (register_quote)."""

    # The RFQ line it answers, as the file names it; null = the file does not say.
    rfq_line_position: int | None
    # The RFQ line's description when the position is one of the RFQ's lines.
    description: str | None = None
    offered_mpn: str | None = None
    is_alternative: bool = False
    quantity: Decimal | None = None
    unit: str | None = None
    unit_price: Decimal | None = None
    currency: str | None = None
    lead_time_days: int | None = None
    # Why the line would need review once stored (missing price, a line the RFQ does
    # not have, a value the file does not back...); empty when it reads cleanly.
    issues: list[str] = Field(default_factory=list[str])


class ProposedOrderLine(BaseModel):
    """One PO line in a purchase-order action's preview: ordered vs received vs this time."""

    position: int
    description: str
    unit: str
    ordered: Decimal
    received_so_far: Decimal
    # The quantity this receipt would add; null for the other actions.
    this_receipt: Decimal | None = None
    remaining_after: Decimal


class ProposedOrderAction(BaseModel):
    """What a purchase-order action (acknowledge, receive, close, cancel) would record."""

    ref: str
    rfq_ref: str
    supplier_name: str
    state: str = Field(description="The order's fulfilment state now")
    state_after: str = Field(description="What it would become")
    lines: list[ProposedOrderLine] = Field(default_factory=list[ProposedOrderLine])
    acknowledged_on: date | None = None
    supplier_order_ref: str | None = None
    received_on: date | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    short: bool = False
    reason: str | None = None
    note: str | None = None
    # Sending a draft order (send_purchase_order): the address it would go to, the total.
    email_to: str | None = None
    total: Decimal | None = None
    currency: str | None = None


class ProposedAwardLine(BaseModel):
    """A line an award would give to a supplier (prepare_award), at its offer's price."""

    position: int
    description: str
    supplier_name: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    currency: str


class ProposedHoldRelease(BaseModel):
    """A supplier's offer the negotiation holds for the buyer (release_negotiation_hold)."""

    supplier_name: str
    offer_version: int
    state: str = Field(description="Where the supplier stands now, in one sentence")
    reason: str = Field(description="Why the offer is held (a code)")
    holds: list[str] = Field(
        default_factory=list[str], description="What on the offer holds it (codes)"
    )


class ProposalPreview(BaseModel):
    """Exactly what the action would do: show all of it to the buyer and ask."""

    action: str
    rfq: str | None = Field(default=None, description="The RFQ it acts on (invites)")
    title: str | None = None
    currency: str | None = None
    lines: list[ProposedLine] = Field(default_factory=list[ProposedLine])
    suppliers: list[ProposedSupplier] = Field(default_factory=list[ProposedSupplier])
    # A quote read from the buyer's file (register_quote): the files, the commercial
    # terms it states and each line it quotes. The supplier is in `suppliers`.
    files: list[str] = Field(default_factory=list[str])
    payment_terms: str | None = None
    valid_until: date | None = None
    incoterm: str | None = None
    quoted: list[QuotedLinePreview] = Field(default_factory=list[QuotedLinePreview])
    skipped: list[str] = Field(
        default_factory=list[str],
        description="Lines left out (a preliminary RFQ: no quantity or unit yet), by part number",
    )
    # Suppliers it would not invite, and why (an RFQ from an assessment).
    skipped_suppliers: list[AssessmentSkippedSupplier] = Field(
        default_factory=list[AssessmentSkippedSupplier]
    )
    # Positions of the lines that would start without a target and maximum unit price.
    lines_without_limits: list[int] = Field(default_factory=list[int])
    # A purchase-order action (`copilot.order_tools`): the order and what would change.
    purchase_order: ProposedOrderAction | None = None
    # A negotiation hold release (`release_negotiation_hold`): the offer and why it waits.
    negotiation_hold: ProposedHoldRelease | None = None
    # An award (`prepare_award`): who would get each line, whether that differs from the
    # recommendation, and the buyer's reason. Confirming only asks for the approval.
    award: list[ProposedAwardLine] = Field(default_factory=list[ProposedAwardLine])
    differs_from_recommendation: bool | None = None
    reason: str | None = None
    questions: list[str] = Field(default_factory=list[str])
    issues: list[str] = Field(
        default_factory=list[str], description="What would stop it, if anything"
    )


class AwaitingConfirmation(BaseModel):
    """Fields every gated tool answers. While `awaiting_confirmation`, nothing was done."""

    awaiting_confirmation: bool = Field(
        default=False,
        description=(
            "True: NOTHING was done yet. Show the preview, ask the buyer to confirm, and "
            "call again with proposal_id after they answer"
        ),
    )
    proposal_id: UUID | None = None
    preview: ProposalPreview | None = None


def created(output: Mapping[str, JsonValue]) -> bool:
    """For `Tool.artifact_if`: a card only once the action ran, not for its proposal."""
    return output.get("awaiting_confirmation") is not True


class ProposalRefused(Exception):
    """A proposal that cannot be confirmed now; the message says why (no content)."""


@dataclass(frozen=True, slots=True)
class Claimed:
    id: UUID
    args: dict[str, Any]
    preview: dict[str, Any]


async def propose(
    db: Db,
    principal: Principal,
    *,
    conversation_id: UUID,
    run_id: UUID,
    action: str,
    args: dict[str, Any],
    preview: dict[str, Any],
) -> UUID:
    """Store what `action` would do; nothing else happens."""
    async with tenant_session(db, principal.tenant_id) as session:
        row = CopilotProposal(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            action=action,
            args=args,
            preview=preview,
        )
        session.add(row)
        await session.flush()
        return row.id


async def claim(
    db: Db,
    principal: Principal,
    *,
    conversation_id: UUID,
    run_id: UUID,
    action: str,
    proposal_id: UUID,
) -> Claimed:
    """The proposal's stored args, marked used, if the buyer may confirm it now.

    Refused when it is not this user's proposal for `action` in this
    conversation, was made in this same run (the buyer has not answered yet),
    was already used, or is older than `PROPOSAL_TTL`.
    """
    async with tenant_session(db, principal.tenant_id) as session:
        claimed = (
            await session.execute(
                update(CopilotProposal)
                .where(
                    CopilotProposal.id == proposal_id,
                    CopilotProposal.user_id == principal.user_id,
                    CopilotProposal.conversation_id == conversation_id,
                    CopilotProposal.action == action,
                    CopilotProposal.run_id != run_id,
                    CopilotProposal.used_at.is_(None),
                    CopilotProposal.created_at > func.now() - PROPOSAL_TTL,
                )
                .values(used_at=func.now())
                .returning(CopilotProposal.id, CopilotProposal.args, CopilotProposal.preview)
            )
        ).one_or_none()
        if claimed is not None:
            return Claimed(id=claimed.id, args=claimed.args, preview=claimed.preview)
        row = await session.scalar(
            select(CopilotProposal).where(
                CopilotProposal.id == proposal_id,
                CopilotProposal.user_id == principal.user_id,
                CopilotProposal.conversation_id == conversation_id,
            )
        )
        expired = row is not None and await session.scalar(
            select(CopilotProposal.created_at <= func.now() - PROPOSAL_TTL).where(
                CopilotProposal.id == proposal_id
            )
        )
    if row is None or row.action != action:
        raise ProposalRefused(
            f"no {action} proposal {proposal_id} in this conversation: call {action} without "
            "proposal_id to propose it again"
        )
    if row.used_at is not None:
        raise ProposalRefused(f"proposal {proposal_id} was already confirmed and done")
    if row.run_id == run_id:
        raise ProposalRefused(
            "the buyer has not confirmed yet: show them the preview, ask, and call again with "
            "proposal_id only after they answer in their own message"
        )
    if expired:
        raise ProposalRefused(
            f"proposal {proposal_id} expired: call {action} without proposal_id to propose it again"
        )
    raise ProposalRefused(f"proposal {proposal_id} cannot be confirmed")


async def release(db: Db, principal: Principal, proposal_id: UUID) -> None:
    """Make a claimed proposal confirmable again, after the action failed."""
    async with tenant_session(db, principal.tenant_id) as session:
        await session.execute(
            update(CopilotProposal)
            .where(
                CopilotProposal.id == proposal_id,
                CopilotProposal.user_id == principal.user_id,
            )
            .values(used_at=None)
        )
