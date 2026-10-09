"""Turn a buyer's award request into the exact lines an approver approves. Pure code.

The source is the RFQ's comparison (`bido_back.comparison`), which already
holds, per supplier, the latest proposal and every priced cell (line x quantity
tier), and per line and tier the recommended offer. A selection names a line,
a tier and optionally the invite (supplier); without an invite the
recommendation is taken. The awarded price is the supplier's own unit price in
the supplier's currency: that is what the purchase order states.

Each line carries the buyer limits its offer breaks (`buyer_limit_issues`: above the
maximum unit price or the lead-time limit): ordering it needs a reason.

Anything that cannot be ordered is refused with a reason a buyer can act on:
an unknown line, a line awarded twice, a supplier that did not price that
tier, or a line with no recommendation and no supplier chosen.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

from bido_back.awards.schemas import AwardLine, AwardSelection
from bido_back.comparison.compute import Cell, Comparison, ComparisonLine, SupplierColumn

# The comparison's buyer-limit flags that an order may only break with a reason
# (`buyer_lead_time_unconfirmed` is shown, not refused: the lead time is unknown).
BREAKS_LIMIT: Final = frozenset({"above_buyer_maximum", "above_buyer_lead_time_limit"})


class SelectionError(ValueError):
    """The award request cannot be turned into orderable lines (message is for the buyer)."""


@dataclass(frozen=True, slots=True)
class Selected:
    lines: list[AwardLine]
    differs_from_recommendation: bool

    @property
    def above_limits(self) -> list[AwardLine]:
        """Lines whose offer breaks a buyer limit (price above the maximum, lead time)."""
        return [
            line for line in self.lines if BREAKS_LIMIT.intersection(line.buyer_limit_issues or ())
        ]


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


def _recommended(comparison: Comparison, line_id: UUID, tier: int) -> UUID | None:
    for rec in comparison.recommendations:
        if rec.rfq_line_id == line_id and rec.tier == tier:
            return rec.invite_id
    return None


def _cell(column: SupplierColumn, line_id: UUID, tier: int) -> Cell | None:
    for cell in column.cells:
        if cell.rfq_line_id == line_id and cell.tier == tier:
            return cell
    return None


def _default_selections(comparison: Comparison) -> list[AwardSelection]:
    """Every line with a recommended offer at tier 0."""
    selections = [
        AwardSelection(rfq_line_id=line.id, tier=0)
        for line in comparison.lines
        if _recommended(comparison, line.id, 0) is not None
    ]
    if not selections:
        raise SelectionError("no line has a recommended offer: choose the suppliers")
    return selections


def _award_line(
    comparison: Comparison,
    line: ComparisonLine,
    tier: int,
    column: SupplierColumn,
    recommended: UUID | None,
) -> AwardLine:
    cell = _cell(column, line.id, tier)
    if cell is None or cell.unit_price is None or cell.proposal_line_id is None:
        raise SelectionError(
            f"line {line.position} tier {tier}: {column.supplier_name} has no price for it"
        )
    currency = cell.currency or column.currency
    if not currency:
        raise SelectionError(
            f"line {line.position}: {column.supplier_name}'s currency is unknown; correct it first"
        )
    return AwardLine(
        rfq_line_id=line.id,
        position=line.position,
        description=line.description,
        mpn=line.mpn,
        tier=tier,
        quantity=_decimal_text(cell.quantity),
        unit=line.unit,
        need_by=line.need_by,
        invite_id=column.invite_id,
        supplier_id=column.supplier_id,
        supplier_name=column.supplier_name,
        proposal_id=column.proposal_id,
        proposal_version=column.proposal_version,
        proposal_line_id=cell.proposal_line_id,
        unit_price=_decimal_text(cell.unit_price),
        currency=currency,
        lead_time_days=cell.lead_time_days,
        recommended_invite_id=recommended,
        buyer_limit_issues=tuple(cell.buyer_limit_issues) or None,
    )


def select_lines(comparison: Comparison, requested: Sequence[AwardSelection]) -> Selected:
    """The lines to award, priced from each supplier's latest proposal."""
    lines = {line.id: line for line in comparison.lines}
    columns = {column.invite_id: column for column in comparison.suppliers}
    selections = list(requested) or _default_selections(comparison)

    awarded: list[AwardLine] = []
    seen: set[UUID] = set()
    differs = False
    for selection in selections:
        line = lines.get(selection.rfq_line_id)
        if line is None:
            raise SelectionError(f"line {selection.rfq_line_id} is not part of this RFQ")
        if line.id in seen:
            raise SelectionError(f"line {line.position} is awarded twice")
        seen.add(line.id)
        if selection.tier >= len(line.quantities):
            raise SelectionError(f"line {line.position} has no quantity tier {selection.tier}")
        recommended = _recommended(comparison, line.id, selection.tier)
        invite_id = selection.invite_id or recommended
        if invite_id is None:
            raise SelectionError(
                f"line {line.position} tier {selection.tier} has no recommended offer: "
                "choose a supplier"
            )
        column = columns.get(invite_id)
        if column is None:
            raise SelectionError(f"invite {invite_id} has no proposal for this RFQ")
        differs = differs or invite_id != recommended
        awarded.append(_award_line(comparison, line, selection.tier, column, recommended))
    awarded.sort(key=lambda a: a.position)
    return Selected(lines=awarded, differs_from_recommendation=differs)
