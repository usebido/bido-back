"""Deterministic supplier offer reconciliation, independent of extraction completion order.

Every supplier reply remains an immutable validated event. Proposal versions contain
an effective snapshot rebuilt in business order. Field evidence retains the original
message and each explicit statement authorizing inheritance. Ambiguity fails closed.
"""

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, JsonValue

from bido_back.agents.catalog.contracts import (
    ExtractedValue,
    ExtraOfferDraft,
    ProposalLineDraft,
    QuoteExtractionOutput,
)
from bido_back.quotes.parsing import detect_currencies, normalize_text
from bido_back.quotes.validation import (
    HEADER_FIELDS,
    LINE_FIELDS,
    FieldCheck,
    FieldKind,
    LineCheck,
    ValidatedQuote,
    absent_field,
)


class StoredField(BaseModel):
    extracted: ExtractedValue
    parsed: str | int | None
    issues: tuple[str, ...]
    message_id: UUID | None = None
    retained_by: tuple[dict[str, JsonValue], ...] = ()
    corrected: bool = False
    correction_id: str | None = None
    conversion: dict[str, JsonValue] | None = None


class StoredLine(BaseModel):
    draft: ProposalLineDraft
    rfq_line_id: UUID | None
    is_alternative: bool
    fields: dict[str, StoredField]
    issues: tuple[str, ...]
    notes: tuple[str, ...]


class StoredQuote(BaseModel):
    output: QuoteExtractionOutput
    header: dict[str, StoredField]
    lines: tuple[StoredLine, ...]
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OfferEvent:
    message_id: UUID
    message_header: str | None
    in_reply_to: str | None
    occurred_at: datetime
    received_at: datetime
    quote: ValidatedQuote
    # The supplier's explicit revision number ("Revisão 2", "Rev. 3", "R01"), if any.
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class OfferState:
    quote: ValidatedQuote
    active_message_id: UUID
    message_ids: tuple[UUID, ...]
    reply_kind: Literal["replacement", "amendment", "withdrawal", "clarification"]
    issues: tuple[str, ...] = ()
    technical_questions: tuple[str, ...] = ()
    withdrawn_line_positions: tuple[int, ...] = ()

    def provenance(self) -> dict[str, JsonValue]:
        return {
            "active_message_id": str(self.active_message_id),
            "message_ids": [str(message_id) for message_id in self.message_ids],
            "reply_kind": self.reply_kind,
            "issues": list(self.issues),
            "requires_clarification": bool(self.issues) or self.reply_kind == "clarification",
            "technical_questions": list(self.technical_questions),
            "withdrawn_line_positions": list(self.withdrawn_line_positions),
            "fully_withdrawn": self.reply_kind == "withdrawal" and not self.quote.lines,
        }


def _store_field(check: FieldCheck) -> StoredField:
    parsed = check.parsed
    return StoredField(
        extracted=check.extracted,
        parsed=parsed.isoformat()
        if isinstance(parsed, date)
        else str(parsed)
        if isinstance(parsed, Decimal)
        else parsed,
        issues=check.issues,
        message_id=check.message_id,
        retained_by=check.retained_by,
        corrected=check.corrected,
        correction_id=check.correction_id,
        conversion=dict(check.conversion) if check.conversion is not None else None,
    )


def store_quote(quote: ValidatedQuote) -> dict[str, JsonValue]:
    return StoredQuote(
        output=quote.output,
        header={name: _store_field(check) for name, check in quote.header.items()},
        lines=tuple(
            StoredLine(
                draft=line.draft,
                rfq_line_id=line.rfq_line_id,
                is_alternative=line.is_alternative,
                fields={name: _store_field(check) for name, check in line.fields.items()},
                issues=line.issues,
                notes=line.notes,
            )
            for line in quote.lines
        ),
        issues=quote.issues,
    ).model_dump(mode="json")


def restore_field(name: str, kind: FieldKind, stored: StoredField) -> FieldCheck:
    parsed = stored.parsed
    if parsed is not None:
        if kind in {"amount", "quantity"}:
            parsed = Decimal(str(parsed))
        elif kind == "date":
            parsed = date.fromisoformat(str(parsed))
        elif kind == "days":
            parsed = int(parsed)
    return FieldCheck(
        name=name,
        extracted=stored.extracted,
        parsed=parsed,
        issues=stored.issues,
        message_id=stored.message_id,
        retained_by=stored.retained_by,
        corrected=stored.corrected,
        correction_id=stored.correction_id,
        conversion=stored.conversion,
    )


def restore_quote(raw: object) -> ValidatedQuote:
    record = StoredQuote.model_validate(raw)
    return ValidatedQuote(
        output=record.output,
        header={
            name: restore_field(name, kind, record.header[name])
            if name in record.header
            else absent_field(name)  # a field the stored quote predates: never stated
            for name, kind in HEADER_FIELDS.items()
        },
        lines=tuple(
            LineCheck(
                draft=line.draft,
                rfq_line_id=line.rfq_line_id,
                is_alternative=line.is_alternative,
                fields={
                    name: restore_field(name, kind, line.fields[name])
                    if name in line.fields
                    else absent_field(name)
                    for name, kind in LINE_FIELDS.items()
                },
                issues=line.issues,
                notes=line.notes,
            )
            for line in record.lines
        ),
        issues=record.issues,
    )


def stamp_quote(quote: ValidatedQuote, message_id: UUID) -> ValidatedQuote:
    return replace(
        quote,
        header={
            name: replace(check, message_id=check.message_id or message_id)
            for name, check in quote.header.items()
        },
        lines=tuple(
            replace(
                line,
                fields={
                    name: replace(check, message_id=check.message_id or message_id)
                    for name, check in line.fields.items()
                },
            )
            for line in quote.lines
        ),
    )


# "Revisão 2", "Revision 3", "Rev. 4", "Revisión 2", "Révision 2", "Revisione 2", "Rev-02".
_REVISION_WORD = re.compile(
    r"\b(?:revis(?:ão|ao|ion|ión|ione|ões|oes)|révision|rev)\b\.?\s*(?:n[º°o.]?\s*)?[-#]?\s*"
    r"(\d{1,3})\b",
    re.IGNORECASE,
)
# "R01", "R2" ending a subject ("Proposta 123 - R01", "Cotação (R2)"); never a part number.
_REVISION_CODE = re.compile(r"(?:^|[\s_(\[])R(\d{1,2})[)\]]?\s*$")


def revision_marker(subject: str | None, body: str | None = None) -> int | None:
    """The one revision number a supplier wrote in its own subject or body, else None.

    Quoted earlier messages (lines starting with `>`) are ignored. Two different
    numbers are no evidence at all.
    """
    found: set[int] = set()
    if subject:
        found.update(int(n) for n in _REVISION_WORD.findall(subject))
        found.update(int(n) for n in _REVISION_CODE.findall(subject))
    if body:
        own = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">"))
        found.update(int(n) for n in _REVISION_WORD.findall(own))
    return next(iter(found)) if len(found) == 1 else None


def _ordered(events: list[OfferEvent]) -> tuple[list[OfferEvent], tuple[str, ...]]:
    """Known reply ancestry wins; receipt time bounds untrusted supplier dates.

    Messages delivered in another order than their Date headers say are
    ordered by those dates only when the supplier's explicit revision numbers
    agree; otherwise the chronology is uncertain and the offer is held.
    """
    by_header = {event.message_header.strip(): event for event in events if event.message_header}
    pending = sorted(
        events,
        key=lambda event: (
            min(event.occurred_at, event.received_at),
            event.revision if event.revision is not None else -1,
            event.received_at,
            str(event.message_id),
        ),
    )
    result: list[OfferEvent] = []
    issues: list[str] = []
    seen: set[UUID] = set()
    if any(event.occurred_at > event.received_at + timedelta(minutes=5) for event in events):
        issues.append("untrusted_future_message_date")
    # No message body or model-supplied reference can create a reply edge.
    while pending:
        progressed = False
        pending_ids = {event.message_id for event in pending}
        for event in pending[:]:
            parent = by_header.get((event.in_reply_to or "").strip())
            if parent is not None and parent.message_id in pending_ids:
                continue
            if event.message_id not in seen:
                result.append(event)
                seen.add(event.message_id)
            pending.remove(event)
            progressed = True
            break
        if not progressed:
            issues.append("cyclic_reply_relationship")
            result.extend(pending)
            break
    for previous, current in zip(result, result[1:], strict=False):
        directly_related = (current.in_reply_to or "").strip() == (
            previous.message_header or ""
        ).strip() and current.in_reply_to is not None
        if directly_related:
            continue
        if (
            previous.revision is not None
            and current.revision is not None
            and previous.revision != current.revision
        ):
            # Two explicit revision numbers: they must agree with the dates.
            if previous.revision > current.revision:
                issues.append("uncertain_message_chronology")
            continue
        if (
            current.received_at < previous.received_at
            or current.occurred_at == previous.occurred_at
        ):
            issues.append("uncertain_message_chronology")
    return result, tuple(dict.fromkeys(issues))


_UNCHANGED = re.compile(
    r"(?:other|remaining|all|same)\s+(?:commercial\s+)?(?:terms|conditions)\s+(?:(?:remain|are|stay)\s+)?unchanged"
    r"|(?:demais|outras|todas\s+as)\s+condi[cç][oõ]es\s+(?:(?:permanecem|seguem|ficam)\s+)?(?:inalteradas|mantidas)"
    r"|mantendo\s+(?:as\s+)?(?:demais|outras)\s+condi[cç][oõ]es"
    r"|(?:alle\s+)?(?:anderen|übrigen)\s+(?:konditionen|bedingungen)\s+(?:bleiben\s+)?unverändert"
    r"|(?:otras|dem[aá]s)\s+condiciones\s+(?:sin\s+cambios|no\s+cambian)"
    r"|autres\s+conditions\s+(?:restent\s+)?inchang[eé]es",
    re.IGNORECASE,
)
_NEGATED_PRESERVATION = re.compile(
    r"\b(?:not|never|unless|except|não|nao|exceto|salvo|nicht|keine|kein|no|ne|pas)\b",
    re.IGNORECASE,
)


def _has_preservation(event: OfferEvent) -> bool:
    quote = event.quote
    evidence = quote.output.unchanged_terms
    return (
        evidence.value is not None
        and evidence.source is not None
        and evidence.confidence >= 0.8
        and "unverified:unchanged_terms" not in quote.issues
        and _UNCHANGED.search(normalize_text(evidence.source.quote)) is not None
        and _NEGATED_PRESERVATION.search(normalize_text(evidence.source.quote)) is None
        and "?" not in evidence.source.quote
    )


def _hold(quote: ValidatedQuote, *issues: str) -> ValidatedQuote:
    combined = tuple(dict.fromkeys((*quote.issues, *issues)))
    return replace(
        quote,
        issues=combined,
        lines=tuple(
            replace(line, issues=tuple(dict.fromkeys((*line.issues, *combined))))
            for line in quote.lines
        ),
    )


def _inherit(previous: FieldCheck, event: OfferEvent) -> FieldCheck:
    source = event.quote.output.unchanged_terms.source
    evidence: dict[str, JsonValue] = {
        "message_id": str(event.message_id),
        "source": source.model_dump(mode="json") if source is not None else None,
    }
    return replace(previous, retained_by=(*previous.retained_by, evidence))


def _merge_fields(
    previous: dict[str, FieldCheck], current: dict[str, FieldCheck], event: OfferEvent
) -> dict[str, FieldCheck]:
    return {
        name: check if check.extracted.value is not None else _inherit(previous[name], event)
        for name, check in current.items()
    }


def _candidate(previous: LineCheck, update: LineCheck) -> bool:
    if previous.rfq_line_id is None or previous.rfq_line_id != update.rfq_line_id:
        return False
    for name in ("min_quantity", "offered_mpn"):
        offered = update.fields[name].parsed
        if offered is not None and offered != previous.fields[name].parsed:
            return False
    return True


def _amend(previous: ValidatedQuote, event: OfferEvent) -> ValidatedQuote:
    current = event.quote
    if not _has_preservation(event):
        return _hold(previous, "amendment_without_explicit_preservation")
    if current.issues:
        return _hold(previous, *current.issues)
    if current.output.conflicts:
        return _hold(previous, "conflicting_update")
    # A new currency/incoterm/payment basis cannot reuse old prices, even if a
    # broad preservation sentence also appears in the supplier's message.
    changed_header = any(
        current.header[name].parsed is not None
        and current.header[name].parsed != previous.header[name].parsed
        for name in ("currency", "incoterm", "payment_terms")
    )
    if changed_header:
        return _hold(previous, "changed_commercial_basis_requires_replacement")
    header = _merge_fields(dict(previous.header), dict(current.header), event)
    lines = list(previous.lines)
    touched: set[int] = set()
    for update in current.lines:
        matches = [index for index, line in enumerate(lines) if _candidate(line, update)]
        if len(matches) != 1 or matches[0] in touched:
            return _hold(previous, "ambiguous_amendment_line_or_tier")
        index = matches[0]
        touched.add(index)
        old = lines[index]
        for name in ("unit_price", "tooling_cost", "freight_cost"):
            check = update.fields[name]
            source = check.extracted.source
            currencies: set[str] = detect_currencies(source.quote) if source is not None else set()
            if currencies and header["currency"].parsed not in currencies:
                return _hold(previous, "changed_commercial_basis_requires_replacement")
        if any(
            update.fields[name].parsed is not None
            and update.fields[name].parsed != old.fields[name].parsed
            for name in ("unit", "price_basis_quantity", "conditional_terms", "moq")
        ):
            return _hold(previous, "changed_commercial_basis_requires_replacement")
        fields = _merge_fields(dict(old.fields), dict(update.fields), event)
        draft = update.draft.model_copy(
            update={name: value.extracted for name, value in fields.items()}
        )
        issues = tuple(dict.fromkeys((*old.issues, *update.issues)))
        # Required fields are evaluated on the resulting state, never the delta.
        issues = tuple(
            issue
            for issue in issues
            if not (issue == "missing:unit_price" and fields["unit_price"].parsed is not None)
        )
        lines[index] = replace(
            update,
            draft=draft,
            fields=fields,
            is_alternative=old.is_alternative or update.is_alternative,
            issues=issues,
        )
    for index, line in enumerate(lines):
        if index not in touched:
            lines[index] = replace(
                line, fields={name: _inherit(check, event) for name, check in line.fields.items()}
            )
    return replace(current, header=header, lines=tuple(lines), issues=previous.issues)


def _extra_key(offer: ExtraOfferDraft) -> str:
    if offer.mpn and offer.mpn.strip():
        return "mpn:" + "".join(c for c in offer.mpn.upper() if c.isalnum())
    return "text:" + normalize_text(offer.description)


def reconcile(events: list[OfferEvent]) -> OfferState:
    if not events:
        raise ValueError("offer events are required")
    ordered, chronology_issues = _ordered(events)
    effective: ValidatedQuote | None = None
    active = ordered[0]
    # Unrequested offers stay until the supplier withdraws its whole offer; a
    # later mention of the same item (same MPN or description) replaces it.
    extras: dict[str, ExtraOfferDraft] = {}
    for raw_event in ordered:
        event = replace(raw_event, quote=stamp_quote(raw_event.quote, raw_event.message_id))
        current = event.quote
        kind = current.output.reply_kind
        active = event
        if (
            kind == "withdrawal"
            and not current.output.withdrawn_line_positions
            and current.output.update_evidence.value is not None
            and "unverified:update_evidence" not in current.issues
        ):
            extras.clear()
        for offer in current.output.extra_offers:
            extras[_extra_key(offer)] = offer
        if kind == "replacement":
            effective = current
        elif kind == "amendment":
            effective = (
                _amend(effective, event)
                if effective is not None
                else _hold(current, "amendment_without_prior_offer")
            )
        elif kind == "withdrawal":
            evidence = current.output.update_evidence
            if (
                evidence.value is None
                or evidence.source is None
                or evidence.confidence < 0.8
                or "unverified:update_evidence" in current.issues
            ):
                effective = _hold(effective or current, "unverified_withdrawal")
            elif effective is not None:
                positions = current.output.withdrawn_line_positions
                known_positions = {line.draft.rfq_line_position for line in effective.lines}
                if any(position not in known_positions for position in positions):
                    effective = _hold(effective, "withdrawal_for_unknown_line")
                    continue
                effective = replace(
                    effective,
                    lines=tuple(
                        line
                        for line in effective.lines
                        if positions and line.draft.rfq_line_position not in positions
                    ),
                )
            else:
                effective = replace(current, lines=())
        else:
            effective = _hold(effective or current, "supplier_clarification_required")
        if current.output.conflicts:
            effective = _hold(effective, "conflicting_sources")
        if current.output.technical_questions:
            effective = _hold(effective, "supplier_clarification_required")
    assert effective is not None
    effective = replace(
        effective,
        output=effective.output.model_copy(update={"extra_offers": list(extras.values())}),
    )
    effective = _hold(effective, *chronology_issues) if chronology_issues else effective
    return OfferState(
        quote=effective,
        active_message_id=active.message_id,
        message_ids=tuple(event.message_id for event in ordered),
        reply_kind=active.quote.output.reply_kind,
        issues=effective.issues,
        technical_questions=tuple(active.quote.output.technical_questions),
        withdrawn_line_positions=tuple(active.quote.output.withdrawn_line_positions),
    )
