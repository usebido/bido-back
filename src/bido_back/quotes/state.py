"""Deterministic supplier offer reconciliation, independent of extraction completion order.

Every supplier reply remains an immutable validated event. Proposal versions contain
an effective snapshot rebuilt in business order. Field evidence retains the original
message and each explicit statement authorizing inheritance. Ambiguity fails closed.

How a reply changes the offer:

- A replacement is the whole new offer: earlier holds and pending updates go.
- An amendment changes what it states and keeps the rest only with an explicit
  "other terms unchanged". It may quote a requested line the offer lacked. Without
  that statement it is held and kept pending: a later amendment that has the
  statement, or a reply to our clarification, covers it, and it is applied first.
- A reply to our clarification (`OfferEvent.answers`, from the ledger in
  `bido_back.followups.ledger`) is applied as a supplement: the fields Bido asked
  for are taken from it, everything else carries over, with no "unchanged"
  sentence. Restating a value changes nothing and filling a gap is fine; changing
  a commercial term nobody asked about (price, currency, unit, basis, MOQ,
  payment, Incoterm, conditions) holds the offer, unless the reply is a whole
  new offer.
- A question-only reply changes nothing and holds nothing: supplier questions
  are tracked in the ledger (`technical_questions` still names them). Nor does a
  reply that only declines requested lines.
- Declined lines (a reply's signals, `ValidatedQuote.signals`, or a withdrawal)
  are kept in `declined_line_positions` until a later reply offers them.
- A buyer release ("use the current offer", `OfferEvent.released`) clears the
  holds up to that reply: the offer as shown is the offer.
- An order deadline ("provided we receive the PO by 21/10/2026") is not a change of
  the commercial basis: validation makes it the offer's `order_by` header field, and
  an amendment applies it like any other stated term.
- A change of the commercial basis an amendment cannot carry (unit, price basis, MOQ,
  conditions, currency, Incoterm, payment terms) holds the offer as it was; each such
  change is kept, with the value before and the value the reply states, in
  `OfferState.held_changes` (`held_changes` in the stored offer state), so a person
  sees what the reply asked to change.

Holds are recomputed on every reply from this event history, never carried as
leftovers: line issues that follow from a field (currency, missing price,
conditions, tax rate) are re-derived from the merged fields, so an answer that
fills a gap also clears the issue it caused.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal, cast
from uuid import UUID

from pydantic import BaseModel, JsonValue

from bido_back.agents.catalog.contracts import (
    ExtractedValue,
    ExtraOfferDraft,
    ProposalLineDraft,
    QuoteExtractionOutput,
)
from bido_back.comparison.compute import same_unit
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


ReplyKind = Literal["replacement", "amendment", "withdrawal", "clarification"]


@dataclass(frozen=True, slots=True)
class AskedField:
    """One item of our clarification that a supplier reply answers.

    `position` is the RFQ line position (None for the whole offer); `field` is the
    ledger's field: currency, valid_until, unit_price, quantity, lead_time,
    delivery_date.
    """

    position: int | None
    field: str

    def provenance(self) -> dict[str, JsonValue]:
        return {"position": self.position, "field": self.field}


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
    # The items of our clarification this reply answers (its first reply with an offer
    # after the email went out); empty for any other reply.
    answers: tuple[AskedField, ...] = ()
    # The buyer released the holds of the offer as it stood after this reply.
    released: bool = False


@dataclass(frozen=True, slots=True)
class HeldChange:
    """A term a reply changed that the offer could not take as it was (or took, and holds
    for the buyer): the field, its line (None for the whole offer), and both values as
    stored (`previous` is None when the offer had none)."""

    message_id: UUID
    field: str
    position: int | None
    previous: str | None
    stated: str | None
    # True when the offer now carries the stated value (an unasked change in a reply to
    # our clarification); False when the offer kept the previous one.
    applied: bool = False

    @property
    def code(self) -> str:
        """`pos1.unit`, `currency`: the field and its line, for labels (no values)."""
        return self.field if self.position is None else f"pos{self.position}.{self.field}"

    def provenance(self) -> dict[str, JsonValue]:
        return {
            "message_id": str(self.message_id),
            "field": self.field,
            "position": self.position,
            "previous": self.previous,
            "stated": self.stated,
            "applied": self.applied,
        }


@dataclass(frozen=True, slots=True)
class OfferState:
    quote: ValidatedQuote
    active_message_id: UUID
    message_ids: tuple[UUID, ...]
    # How the effective offer was last set: a reply to our clarification is an
    # amendment; a question-only reply does not change it.
    reply_kind: ReplyKind
    issues: tuple[str, ...] = ()
    technical_questions: tuple[str, ...] = ()
    withdrawn_line_positions: tuple[int, ...] = ()
    # The clarification items the latest reply answered (applied as a supplement).
    answered: tuple[AskedField, ...] = ()
    # Positions the supplier withdrew (or declined) and has not offered since.
    declined_line_positions: tuple[int, ...] = ()
    # The replies whose holds are still active (not applied, or applied with a doubt).
    held_message_ids: tuple[UUID, ...] = ()
    # What those replies changed in the commercial basis (see `HeldChange`).
    held_changes: tuple[HeldChange, ...] = ()

    def provenance(self) -> dict[str, JsonValue]:
        return {
            "active_message_id": str(self.active_message_id),
            "message_ids": [str(message_id) for message_id in self.message_ids],
            "reply_kind": self.reply_kind,
            "issues": list(self.issues),
            "requires_clarification": bool(self.issues)
            or self.reply_kind == "clarification"
            or bool(self.technical_questions),
            "technical_questions": list(self.technical_questions),
            "withdrawn_line_positions": list(self.withdrawn_line_positions),
            "fully_withdrawn": self.reply_kind == "withdrawal" and not self.quote.lines,
            "answered_asks": [answer.provenance() for answer in self.answered],
            "declined_line_positions": list(self.declined_line_positions),
            "held_message_ids": [str(message_id) for message_id in self.held_message_ids],
            "held_changes": [change.provenance() for change in self.held_changes],
        }


def released_message_id(offer_state: object) -> UUID | None:
    """The reply whose holds the buyer released, from a proposal's `_offer_state`."""
    if not isinstance(offer_state, Mapping):
        return None
    released = cast(Mapping[str, object], offer_state).get("released")
    if not isinstance(released, Mapping):
        return None
    raw = cast(Mapping[str, object], released).get("message_id")
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


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


def _ordered(events: list[OfferEvent]) -> tuple[list[OfferEvent], list[tuple[str, int]]]:
    """Known reply ancestry wins; receipt time bounds untrusted supplier dates.

    Messages delivered in another order than their Date headers say are
    ordered by those dates only when the supplier's explicit revision numbers
    agree; otherwise the chronology is uncertain and the offer is held. Each
    issue comes with the position (in the order) of the latest reply it involves.
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
    cyclic: list[tuple[str, int]] = []
    seen: set[UUID] = set()
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
            result.extend(pending)
            cyclic.append(("cyclic_reply_relationship", len(result) - 1))
            break
    issues = [
        ("untrusted_future_message_date", index)
        for index, event in enumerate(result)
        if event.occurred_at > event.received_at + timedelta(minutes=5)
    ]
    issues.extend(cyclic)
    for index, (previous, current) in enumerate(zip(result, result[1:], strict=False), start=1):
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
                issues.append(("uncertain_message_chronology", index))
            continue
        if (
            current.received_at < previous.received_at
            or current.occurred_at == previous.occurred_at
        ):
            issues.append(("uncertain_message_chronology", index))
    return result, issues


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


def _inherit(previous: FieldCheck, event: OfferEvent, basis: str | None = None) -> FieldCheck:
    """`previous` carried over by `event`; the evidence says why it may be."""
    source = event.quote.output.unchanged_terms.source
    evidence: dict[str, JsonValue] = {
        "message_id": str(event.message_id),
        "source": source.model_dump(mode="json") if source is not None else None,
    }
    if basis is not None:
        evidence["basis"] = basis
    return replace(previous, retained_by=(*previous.retained_by, evidence))


def _merge_fields(
    previous: Mapping[str, FieldCheck],
    current: Mapping[str, FieldCheck],
    event: OfferEvent,
    basis: str | None = None,
) -> dict[str, FieldCheck]:
    return {
        name: check if check.extracted.value is not None else _inherit(previous[name], event, basis)
        for name, check in current.items()
    }


def _tier_matches(previous: LineCheck, update: LineCheck, *, only_tier: bool) -> bool:
    """`update` restates `previous`'s price break. A break the offer left without a
    quantity is matched by position when it is the line's only break."""
    if previous.rfq_line_id is None or previous.rfq_line_id != update.rfq_line_id:
        return False
    offered_mpn = update.fields["offered_mpn"].parsed
    if offered_mpn is not None and offered_mpn != previous.fields["offered_mpn"].parsed:
        return False
    offered = update.fields["min_quantity"].parsed
    stored = previous.fields["min_quantity"].parsed
    return offered is None or offered == stored or (stored is None and only_tier)


def _matches(lines: Sequence[LineCheck], update: LineCheck) -> list[int]:
    """The previous price breaks `update` may restate (exactly one, unless ambiguous)."""
    same_line = [
        index
        for index, line in enumerate(lines)
        if line.rfq_line_id is not None and line.rfq_line_id == update.rfq_line_id
    ]
    only_tier = len(same_line) == 1
    return [i for i in same_line if _tier_matches(lines[i], update, only_tier=only_tier)]


def _new_line(lines: Sequence[LineCheck], update: LineCheck) -> bool:
    """`update` quotes a requested line the offer had no price break for at all."""
    return update.rfq_line_id is not None and not any(
        line.rfq_line_id == update.rfq_line_id for line in lines
    )


def _other_currencies(update: LineCheck, header: Mapping[str, FieldCheck]) -> tuple[str, ...]:
    """The currencies a price's text names when none is the offer's (empty: no change)."""
    for name in _PRICED:
        source = update.fields[name].extracted.source
        currencies: set[str] = detect_currencies(source.quote) if source is not None else set()
        if currencies and header["currency"].parsed not in currencies:
            return tuple(sorted(currencies))
    return ()


def _currency_change(
    event: OfferEvent, update: LineCheck, header: Mapping[str, FieldCheck]
) -> HeldChange | None:
    """A line price in another currency than the offer's, as a held change."""
    named = _other_currencies(update, header)
    if not named:
        return None
    previous = header["currency"].parsed
    return HeldChange(
        message_id=event.message_id,
        field="currency",
        position=update.draft.rfq_line_position,
        previous=_stored(previous),
        stated=",".join(named),
    )


def _merged_line(old: LineCheck, update: LineCheck, fields: dict[str, FieldCheck]) -> LineCheck:
    extracted = {name: value.extracted for name, value in fields.items()}
    draft = update.draft.model_copy(update=extracted)
    return replace(
        update,
        draft=draft,
        fields=fields,
        is_alternative=old.is_alternative or update.is_alternative,
        issues=tuple(dict.fromkeys((*old.issues, *update.issues))),
        notes=tuple(dict.fromkeys((*old.notes, *update.notes))),
    )


def _stored(value: object) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, date) else str(value)


def _basis_changed(name: str, old: FieldCheck, new: FieldCheck) -> bool:
    """`new` states another value of a commercial-basis field than `old` (the same unit
    or condition in other words, "pcs" for "piece", is no change)."""
    if new.parsed is None or new.parsed == old.parsed:
        return False
    if name == "unit" and isinstance(old.parsed, str) and isinstance(new.parsed, str):
        return not same_unit(old.parsed, new.parsed)
    if isinstance(old.parsed, str) and isinstance(new.parsed, str):
        return normalize_text(old.parsed) != normalize_text(new.parsed)
    return True


def _change(
    event: OfferEvent,
    name: str,
    old: FieldCheck | None,
    new: FieldCheck,
    position: int | None,
    *,
    applied: bool = False,
) -> HeldChange:
    return HeldChange(
        message_id=event.message_id,
        field=name,
        position=position,
        previous=_stored(old.parsed) if old is not None else None,
        stated=_stored(new.parsed),
        applied=applied,
    )


_LINE_BASIS: Final = ("unit", "price_basis_quantity", "conditional_terms", "moq")
_AmendResult = tuple[ValidatedQuote, tuple[str, ...], tuple[HeldChange, ...]]


def _amend(
    previous: ValidatedQuote, event: OfferEvent, *, authority: OfferEvent | None = None
) -> _AmendResult:
    """Apply an amendment: the merged offer, or `previous`, the reasons to hold and the
    basis changes that hold it.

    `authority` is the later reply that covers this one (an amendment with an
    "other terms unchanged" statement, or answers to our clarification) when this
    one had no such statement of its own.
    """
    current = event.quote
    if authority is None and not _has_preservation(event):
        return previous, ("amendment_without_explicit_preservation",), ()
    issues = tuple(
        issue
        for issue in current.issues
        if not (authority is not None and issue == "unverified:unchanged_terms")
    )
    if issues:
        return previous, issues, ()
    if current.output.conflicts:
        return previous, ("conflicting_update",), ()
    # A new currency/incoterm/payment basis cannot reuse old prices, even if a
    # broad preservation sentence also appears in the supplier's message.
    changed_header = tuple(
        _change(event, name, previous.header[name], current.header[name], None)
        for name in _COMMERCIAL_HEADER
        if _basis_changed(name, previous.header[name], current.header[name])
    )
    if changed_header:
        return previous, (_UNASKED_CHANGE,), changed_header
    basis = f"covered_by:{authority.message_id}" if authority is not None else None
    header = _merge_fields(previous.header, current.header, event, basis)
    lines = list(previous.lines)
    touched: set[int] = set()
    added: list[LineCheck] = []
    for update in current.lines:
        position = update.draft.rfq_line_position
        if (currency := _currency_change(event, update, header)) is not None:
            return previous, (_UNASKED_CHANGE,), (currency,)
        if _new_line(lines, update):
            # A requested line the offer lacked ("the shaft, other terms unchanged").
            added.append(update)
            continue
        matches = _matches(lines, update)
        if len(matches) != 1 or matches[0] in touched:
            return previous, ("ambiguous_amendment_line_or_tier",), ()
        index = matches[0]
        touched.add(index)
        old = lines[index]
        changed = tuple(
            _change(event, name, old.fields[name], update.fields[name], position)
            for name in _LINE_BASIS
            if _basis_changed(name, old.fields[name], update.fields[name])
        )
        if changed:
            return previous, (_UNASKED_CHANGE,), changed
        lines[index] = _merged_line(old, update, _merge_fields(old.fields, update.fields, event))
    for index, line in enumerate(lines):
        if index not in touched:
            lines[index] = replace(
                line,
                fields={name: _inherit(check, event, basis) for name, check in line.fields.items()},
            )
    merged = replace(current, header=header, lines=(*lines, *added), issues=previous.issues)
    return merged, (), ()


# --- Answers to our clarification -------------------------------------------------------------

# The quote fields an asked item lets the answer set.
_ANSWERED_HEADER: Final[Mapping[str, tuple[str, ...]]] = {
    "currency": ("currency",),
    "valid_until": ("valid_until", "validity"),
}
_ANSWERED_LINE: Final[Mapping[str, tuple[str, ...]]] = {
    "unit_price": ("unit_price", "unit", "price_basis_quantity"),
    "quantity": ("min_quantity", "price_basis_quantity"),
    "lead_time": ("lead_time_days", "lead_time_terms"),
    "delivery_date": ("lead_time_days", "lead_time_terms"),
}
# The offer's commercial basis: a reply may not change these unasked.
_COMMERCIAL_HEADER: Final = ("currency", "incoterm", "payment_terms")
_COMMERCIAL_LINE: Final = (
    "unit_price",
    "unit",
    "price_basis_quantity",
    "conditional_terms",
    "moq",
    "tooling_cost",
    "freight_cost",
    "tax_rate_percent",
)
_PRICED: Final = ("unit_price", "tooling_cost", "freight_cost")
_SUPPLEMENT: Final = "clarification_answer"


def _restates(old: FieldCheck, new: FieldCheck) -> bool:
    if old.parsed is None or new.parsed is None:
        return False
    if isinstance(old.parsed, str) and isinstance(new.parsed, str):
        return normalize_text(old.parsed) == normalize_text(new.parsed)
    return old.parsed == new.parsed


def _supplement_field(
    old: FieldCheck,
    new: FieldCheck,
    event: OfferEvent,
    *,
    asked: bool,
    commercial: bool,
    whole_offer: bool,
) -> tuple[FieldCheck, bool]:
    """The field after a reply to our clarification, and whether the reply changed a
    commercial term nobody asked about (taken, but the offer is held for the buyer)."""
    if new.extracted.value is None:
        return _inherit(old, event, _SUPPLEMENT), False
    if _restates(old, new):
        # The same value again: keep whichever evidence needs no review.
        return (old if new.issues and not old.issues else new), False
    if asked or (old.parsed is None and old.extracted.value is None):
        return new, False
    return new, commercial and not whole_offer


_UNASKED_CHANGE: Final = "changed_commercial_basis_requires_replacement"


def _supplement(
    previous: ValidatedQuote, event: OfferEvent
) -> tuple[ValidatedQuote, tuple[str, ...], bool, tuple[HeldChange, ...]]:
    """A reply to our clarification, applied as an amendment of exactly what was asked:
    (the offer, the reasons to hold it, whether the reply was applied, the commercial
    terms it changed unasked).

    The asked fields are taken from the reply; everything else carries over, with no
    "other terms unchanged" sentence. Restated values and filled gaps change nothing
    doubtful. A commercial term changed unasked is taken too, as the supplier's latest
    statement, but holds the offer for the buyer, unless the reply is a whole new offer
    (which states every term it changes). Conflicting statements, or a price break that
    cannot be told apart, leave the offer as it was, held.
    """
    current = event.quote
    if current.output.conflicts or any(
        issue.startswith("conflicting_sources") for issue in current.issues
    ):
        return previous, ("conflicting_update",), False, ()
    whole_offer = current.output.reply_kind == "replacement"
    unasked: list[HeldChange] = []
    header_asked = {
        name
        for answer in event.answers
        if answer.position is None
        for name in _ANSWERED_HEADER.get(answer.field, ())
    }
    line_asked: dict[int, set[str]] = {}
    for answer in event.answers:
        if answer.position is not None:
            line_asked.setdefault(answer.position, set()).update(
                _ANSWERED_LINE.get(answer.field, ())
            )
    header: dict[str, FieldCheck] = {}
    for name, new in current.header.items():
        header[name], changed = _supplement_field(
            previous.header[name],
            new,
            event,
            asked=name in header_asked,
            commercial=name in _COMMERCIAL_HEADER,
            whole_offer=whole_offer,
        )
        if changed:
            unasked.append(_change(event, name, previous.header[name], new, None, applied=True))
    lines = list(previous.lines)
    touched: set[int] = set()
    added: list[LineCheck] = []
    for update in current.lines:
        if update.rfq_line_id is None:
            return previous, ("ambiguous_amendment_line_or_tier",), False, ()
        if (currency := _currency_change(event, update, header)) is not None:
            return previous, (_UNASKED_CHANGE,), False, (currency,)
        if _new_line(lines, update):
            added.append(update)
            continue
        matches = _matches(lines, update)
        if len(matches) != 1 or matches[0] in touched:
            return previous, ("ambiguous_amendment_line_or_tier",), False, ()
        index = matches[0]
        touched.add(index)
        old = lines[index]
        position = update.draft.rfq_line_position
        asked = line_asked.get(position, set()) if position is not None else set[str]()
        fields: dict[str, FieldCheck] = {}
        for name, new in update.fields.items():
            fields[name], changed = _supplement_field(
                old.fields[name],
                new,
                event,
                asked=name in asked,
                commercial=name in _COMMERCIAL_LINE,
                whole_offer=whole_offer,
            )
            if changed:
                unasked.append(_change(event, name, old.fields[name], new, position, applied=True))
        lines[index] = _merged_line(old, update, fields)
    for index, line in enumerate(lines):
        if index not in touched:
            lines[index] = replace(
                line,
                fields={
                    name: _inherit(check, event, _SUPPLEMENT) for name, check in line.fields.items()
                },
            )
    merged = replace(current, header=header, lines=(*lines, *added), issues=previous.issues)
    return merged, (_UNASKED_CHANGE,) if unasked else (), True, tuple(unasked)


# --- Issues that follow from the fields -------------------------------------------------------


def _resolved_line_issues(line: LineCheck, currency: FieldCheck) -> tuple[str, ...]:
    """The line's issues without those its merged fields no longer cause.

    Only resolves: an issue goes once its cause is gone (a price now given, a trusted
    currency, conditions or an impossible tax rate gone); nothing is added here.
    """
    priced = [line.fields[name] for name in _PRICED if line.fields[name].parsed is not None]
    trusted = currency.parsed is not None and not currency.needs_review
    kept: list[str] = []
    for issue in line.issues:
        if issue == "missing:unit_price" and line.fields["unit_price"].parsed is not None:
            continue
        if issue == "currency_unverified" and (trusted or not priced):
            continue
        if issue.startswith("currency_mismatch:"):
            check = line.fields.get(issue.partition(":")[2])
            source = check.extracted.source if check is not None else None
            named = detect_currencies(source.quote) if source is not None else set[str]()
            if check is None or check.parsed is None or not named or currency.parsed in named:
                continue
        if (
            issue == "conditional_terms_require_review"
            and line.fields["conditional_terms"].parsed is None
        ):
            continue
        tax = line.fields["tax_rate_percent"].parsed
        if issue == "invalid_tax_rate" and not (isinstance(tax, Decimal) and tax > 100):
            continue
        kept.append(issue)
    return tuple(kept)


def _resolved(
    quote: ValidatedQuote, issues_by_message: Mapping[UUID, tuple[str, ...]]
) -> ValidatedQuote:
    """The effective quote without the issues its fields no longer cause."""
    currency = quote.header["currency"]
    kept: list[str] = []
    for issue in quote.issues:
        if issue == "missing:currency" and currency.parsed is not None:
            continue
        if issue == "no_lines" and quote.lines:
            continue
        if issue == "validity_expired":
            # The validity in force came from a reply that was not expired.
            source = quote.header["valid_until"].message_id
            if source is not None and "validity_expired" not in issues_by_message.get(
                source, ("validity_expired",)
            ):
                continue
        kept.append(issue)
    return replace(
        quote,
        issues=tuple(kept),
        lines=tuple(
            replace(line, issues=_resolved_line_issues(line, currency)) for line in quote.lines
        ),
    )


def _extra_key(offer: ExtraOfferDraft) -> str:
    if offer.mpn and offer.mpn.strip():
        return "mpn:" + "".join(c for c in offer.mpn.upper() if c.isalnum())
    return "text:" + normalize_text(offer.description)


def has_offer_content(quote: ValidatedQuote) -> bool:
    """The reply states something about the offer: a line, or a header value."""
    return bool(quote.lines) or any(
        check.extracted.value is not None for check in quote.header.values()
    )


def answers_offer(event: OfferEvent, effective: ValidatedQuote | None) -> bool:
    """`event` answers our clarification about an offer: it is applied as a supplement."""
    return (
        bool(event.answers)
        and effective is not None
        and event.quote.output.reply_kind != "withdrawal"
        and has_offer_content(event.quote)
    )


def _question_only(quote: ValidatedQuote) -> bool:
    """A reply that only asks the buyer something and/or declines requested lines it never
    priced: nothing in the offer changes and nothing is held (the questions wait in the
    ledger, the declined lines are never asked for)."""
    output = quote.output
    return (
        output.reply_kind == "clarification"
        and bool(output.technical_questions or quote.signals.declined_lines)
        and not output.conflicts
        and not has_offer_content(quote)
    )


def _offered_positions(quote: ValidatedQuote) -> set[int]:
    return {
        line.draft.rfq_line_position
        for line in quote.lines
        if line.draft.rfq_line_position is not None
    }


@dataclass(slots=True)
class _Progress:
    """The offer while the replies are replayed in business order."""

    effective: ValidatedQuote | None = None
    kind: ReplyKind = "replacement"
    # (reason, the reply that caused it), in order.
    holds: list[tuple[str, UUID]] = field(default_factory=list[tuple[str, UUID]])
    # Amendments held only for lack of an "other terms unchanged" statement.
    pending: list[OfferEvent] = field(default_factory=list[OfferEvent])
    declined: set[int] = field(default_factory=set[int])
    # The basis changes behind the holds, in order.
    changes: list[HeldChange] = field(default_factory=list[HeldChange])

    def hold(self, event: OfferEvent, *reasons: str) -> None:
        self.holds.extend((reason, event.message_id) for reason in reasons)

    def clear(self) -> None:
        self.holds = []
        self.pending = []
        self.changes = []

    def apply_pending(self, authority: OfferEvent) -> None:
        """Earlier amendments held only for a missing "other terms unchanged", now covered
        by `authority`: applied first, in order."""
        for earlier in self.pending:
            assert self.effective is not None
            self.holds = [
                (reason, message_id)
                for reason, message_id in self.holds
                if not (
                    message_id == earlier.message_id
                    and reason == "amendment_without_explicit_preservation"
                )
            ]
            self.effective, reasons, changes = _amend(self.effective, earlier, authority=authority)
            self.hold(earlier, *reasons)
            self.changes.extend(changes)
        self.pending = []

    def apply(self, event: OfferEvent) -> bool:
        """One reply (see the module docstring). True when what it states is now in the
        offer (a supplement may be applied and still hold the offer)."""
        current = event.quote
        kind = current.output.reply_kind
        if answers_offer(event, self.effective):
            self.apply_pending(event)
            assert self.effective is not None
            self.effective, reasons, applied, changes = _supplement(self.effective, event)
            self.hold(event, *reasons)
            self.changes.extend(changes)
            if applied:
                self.kind = "amendment"
            return applied
        if kind == "replacement":
            self.effective = current
            self.kind = "replacement"
            self.clear()
            return True
        if kind == "amendment":
            return self._amendment(event)
        if kind == "withdrawal":
            self._withdrawal(event)
            return False
        if self.effective is not None and _question_only(current):
            # A question changes nothing: it waits for the buyer in the ledger.
            return False
        self.effective = self.effective or current
        self.kind = "clarification"
        self.hold(event, "supplier_clarification_required")
        return False

    def _amendment(self, event: OfferEvent) -> bool:
        if self.effective is None:
            self.effective = event.quote
            self.kind = "amendment"
            self.hold(event, "amendment_without_prior_offer")
            return False
        if not _has_preservation(event):
            self.hold(event, "amendment_without_explicit_preservation")
            self.pending.append(event)
            return False
        self.apply_pending(event)
        self.effective, reasons, changes = _amend(self.effective, event)
        self.hold(event, *reasons)
        self.changes.extend(changes)
        if reasons:
            return False
        self.kind = "amendment"
        return True

    def _withdrawal(self, event: OfferEvent) -> None:
        current = event.quote
        evidence = current.output.update_evidence
        if (
            evidence.value is None
            or evidence.source is None
            or evidence.confidence < 0.8
            or "unverified:update_evidence" in current.issues
        ):
            self.effective = self.effective or current
            self.hold(event, "unverified_withdrawal")
            return
        if self.effective is None:
            self.effective = replace(current, lines=())
            self.kind = "withdrawal"
            return
        positions = current.output.withdrawn_line_positions
        known_positions = {line.draft.rfq_line_position for line in self.effective.lines}
        if any(position not in known_positions for position in positions):
            self.hold(event, "withdrawal_for_unknown_line")
            return
        withdrawn = set(positions) if positions else _offered_positions(self.effective)
        self.effective = replace(
            self.effective,
            lines=tuple(
                line
                for line in self.effective.lines
                if positions and line.draft.rfq_line_position not in positions
            ),
        )
        self.kind = "withdrawal"
        self.declined |= withdrawn


def reconcile(events: list[OfferEvent]) -> OfferState:
    if not events:
        raise ValueError("offer events are required")
    ordered, chronology_issues = _ordered(events)
    progress = _Progress(kind=ordered[0].quote.output.reply_kind)
    active = ordered[0]
    answered: tuple[AskedField, ...] = ()
    released_at = -1
    issues_by_message: dict[UUID, tuple[str, ...]] = {}
    # Unrequested offers stay until the supplier withdraws its whole offer; a
    # later mention of the same item (same MPN or description) replaces it.
    extras: dict[str, ExtraOfferDraft] = {}
    for index, raw_event in enumerate(ordered):
        event = replace(raw_event, quote=stamp_quote(raw_event.quote, raw_event.message_id))
        current = event.quote
        kind = current.output.reply_kind
        active = event
        issues_by_message[event.message_id] = current.issues
        if (
            kind == "withdrawal"
            and not current.output.withdrawn_line_positions
            and current.output.update_evidence.value is not None
            and "unverified:update_evidence" not in current.issues
        ):
            extras.clear()
        for offer in current.output.extra_offers:
            extras[_extra_key(offer)] = offer
        supplement = answers_offer(event, progress.effective)
        applied = progress.apply(event)
        answered = event.answers if supplement and applied else ()
        if applied:
            progress.declined -= _offered_positions(current)
        # Lines the reply says the supplier will not quote (its signals): never asked for,
        # until a later reply offers them.
        progress.declined |= set(current.signals.declined_line_positions)
        if current.output.conflicts:
            progress.hold(event, "conflicting_sources")
        if event.released:
            # The buyer took the offer as it stood after this reply: its holds go.
            progress.clear()
            if progress.kind == "clarification":
                progress.kind = "amendment"
            released_at = index
    effective = progress.effective
    assert effective is not None
    effective = replace(
        effective,
        output=effective.output.model_copy(update={"extra_offers": list(extras.values())}),
    )
    effective = _resolved(effective, issues_by_message)
    reasons = [reason for reason, _ in progress.holds]
    reasons.extend(issue for issue, at in chronology_issues if at > released_at)
    if reasons:
        effective = _hold(effective, *dict.fromkeys(reasons))
    return OfferState(
        quote=effective,
        active_message_id=active.message_id,
        message_ids=tuple(event.message_id for event in ordered),
        reply_kind=progress.kind,
        issues=effective.issues,
        technical_questions=tuple(active.quote.output.technical_questions),
        withdrawn_line_positions=tuple(active.quote.output.withdrawn_line_positions),
        answered=answered,
        declined_line_positions=tuple(sorted(progress.declined)),
        held_message_ids=tuple(dict.fromkeys(message_id for _, message_id in progress.holds)),
        held_changes=tuple(progress.changes),
    )
