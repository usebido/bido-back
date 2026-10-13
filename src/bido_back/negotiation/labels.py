"""What the buyer reads about a supplier's negotiation: one English sentence, from codes.

Shared by the negotiation API, the activity feed and the Copilot. Built only
from the planner's codes, positions and dates: never supplier text, prices or
names, so a label is always safe to log, show or hand to a model.

Every label starts with the phase in words the buyer recognizes:
"Waiting for the supplier ...", "Waiting until ...", "Held for your approval
...", "Accepted internally – nothing to ask ...", "Needs your decision ...",
"Needs your answer ...", "Closed ...".
"""

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Final

from bido_back.negotiation.planner import LinePlan, PlanWarning, SupplierPlan

# Why an offered line cannot be negotiated as it is (comparison blockers), in words.
_BLOCKERS: Final[dict[str, str]] = {
    "alternative_needs_approval": "an alternative part is offered",
    "unauthorized_source": "the supplier is not an authorized source for this part",
    "validity_expired": "the offer has expired",
    "needs_review": "the offer needs your review",
    "low_confidence": "values read with low confidence need your review",
    "low_confidence_currency": "the currency needs your review",
    "conditional_terms_require_review": "the price has conditions to review",
    "missing_currency": "no currency was given",
    "fx_rate_missing": "no exchange rate for its currency",
    "missing_unit": "no unit was given",
    "unit_mismatch": "a different unit than requested",
    "below_min_quantity": "the requested quantity is below its price breaks",
    "not_comparable": "the price cannot be compared",
    "delivery_after_required_date": "the delivery is after the required date",
    "delivery_date_unconfirmed": "the delivery date is unconfirmed",
    "complete_costs_required": "the complete costs are missing",
    "delivery_history_below_policy": "the supplier's delivery history is below your policy",
    "quality_history_below_policy": "the supplier's quality history is below your policy",
    "delivery_history_insufficient": "too little delivery history for your policy",
    "quality_history_insufficient": "too little quality history for your policy",
}

_REASONS: Final[dict[str, str]] = {
    # Waiting for the supplier.
    "awaiting_quote": "Waiting for the supplier's quote",
    "awaiting_quote_reminders_exhausted": "Waiting for the supplier: no reply after the "
    "reminders sent (no more reminders)",
    "quote_overdue": "Waiting for the supplier: the quote is overdue",
    "clarification_sent": "Waiting for the supplier's answers to our questions",
    "revised_offer_sent": "Waiting for the supplier's revised offer",
    "delivery_date_sent": "Waiting for the supplier to confirm a delivery date",
    "bafo_sent": "Waiting for the supplier's best and final offer",
    "reminder_sent": "Waiting for the supplier: reminded of our request (the one reminder)",
    "reply_overdue": "Waiting for the supplier: no reply to our last request; its current "
    "offer stands",
    # Waiting for Bido to read the supplier's reply (quote extraction).
    "reading_reply": "Waiting: Bido is reading the supplier's reply",
    # Waiting for Bido's own email.
    "preparing_email": "Waiting: Bido is preparing the next email to the supplier",
    "daily_limit": "Waiting: the next email is ready and goes out after the daily email limit",
    "mailbox_not_connected": "Waiting: the next email goes out once your mailbox is connected",
    "clarification_due": "Waiting: Bido is preparing questions for the supplier",
    "clarification_daily_limit": "Waiting: questions for the supplier go out after the daily "
    "email limit",
    "revised_offer_due": "Waiting: Bido is preparing a request for a revised offer",
    "delivery_date_due": "Waiting: Bido is preparing a delivery-date request",
    "bafo_due": "Waiting: Bido is preparing the best-and-final request",
    "revised_offer_with_questions_due": "Waiting: Bido is preparing one email asking for "
    "what the offer lacks and for a revised offer",
    "delivery_date_with_questions_due": "Waiting: Bido is preparing one email asking for "
    "what the offer lacks and for a delivery date",
    "bafo_with_questions_due": "Waiting: Bido is preparing one email asking for what the "
    "offer lacks and for the best and final offer",
    "reminder_due": "Waiting: no reply by the reply-by date; Bido is preparing one reminder of "
    "our request",
    # Held for a person.
    "email_awaiting_approval": "Held for your approval: the next email to the supplier",
    "clarification_awaiting_approval": "Held for your approval: questions to the supplier",
    "clarification_held_by_mandate": "Held for your approval: questions to the supplier are "
    "outside the negotiation mandate",
    "mandate_paused": "Held for your approval: the negotiation mandate is paused",
    "rfq_email:email_awaiting_approval": "Held for your approval: the RFQ email",
    "rfq_email:mailbox_not_connected": "Waiting: the RFQ email goes out once your mailbox is "
    "connected",
    "rfq_email:daily_limit": "Waiting: the RFQ email goes out after the daily email limit",
    "rfq_email:mandate_paused": "Held for your approval: the RFQ email (mandate paused)",
    # Accepted.
    "nothing_to_ask": "Accepted internally – nothing to ask: every quoted line is within "
    "target, on time and the validity is known",
    "final_offer_in_target": "Accepted internally – nothing to ask: the final offer is within "
    "target and on time",
    # The buyer decides.
    "no_progress": "Needs your decision: the supplier did not improve after our request, so "
    "Bido stopped asking",
    "email_not_prepared": "Needs your decision: the next email to the supplier could not be "
    "prepared",
    "email_rejected": "Needs your decision: you rejected the proposed email",
    "offer_state_requires_review": "Needs your decision: the supplier's last reply could not "
    "be applied to its offer; review it",
    "supplier_clarification_requires_buyer": "Needs your decision: the supplier's last reply "
    "needs your review",
    "delivery_still_late": "Needs your decision: still late after our delivery-date request",
    "delivery_late_final": "Needs your decision: the supplier's final offer delivers after the "
    "need-by date",
    "delivery_need_by_unreachable": "Needs your decision: the need-by date can no longer be met",
    "delivery_unknown": "Needs your decision: the lead time is unknown",
    "clarification_unanswered": "Needs your decision: questions to the supplier remain unanswered",
    "price_meets_last_ask": "Needs your decision: the price meets our last request but is "
    "above target",
    "moq_above_quantity": "Needs your decision: the minimum order quantity is above the "
    "requested quantity",
    "validity_unknown": "Needs your decision: the offer's validity is unknown",
    "validity_ends_before_decision": "Needs your decision: the offer expires before the "
    "negotiation ends",
    "payment_terms_shorter": "Needs your decision: the payment terms are shorter than the RFQ "
    "asked",
    "order_deadline_passed": "Needs your decision: the supplier's order deadline has passed, so "
    "its offer no longer holds as stated",
    "offer_blocked": "Needs your decision: the offer cannot be negotiated as it is",
    "no_priced_line": "Needs your decision: the reply has no price for any line",
    "needs_review": "Needs your decision: the offer needs your review",
    # The buyer answers.
    "supplier_question": "Needs your answer: the supplier asked a question; Bido is waiting "
    "for you",
    "reply_without_offer": "Needs your answer: the supplier replied without an offer (a "
    "question or a request); Bido is waiting for you",
    # Closed.
    "negotiation_closed": "Closed: the negotiation is closed",
    "negotiation_not_started": "Not started: the negotiation has not been opened",
    "rfq_not_sent": "Not started: the RFQ email was not sent yet",
    "supplier_declined": "Closed: the supplier declined",
    "offer_withdrawn": "Closed: the supplier withdrew its offer",
    "bafo_answered": "Closed: best and final offer received; it stands for your decision",
    "supplier_declared_final": "Closed: the supplier sent its final offer; it stands for your "
    "decision",
    "final_round_ended": "Closed: no answer to the best-and-final request by the cut-off; the "
    "last offer stands",
    "round_limit_reached": "Closed: round limit reached; the last offer stands",
    "no_reply_after_reminder": "Closed: no reply to our request after a reminder; the last "
    "offer stands",
    "final_round_closed": "Closed: the final round's cut-off passed; the last offer stands",
    "no_time_for_final_round": "Closed: too close to the deadline for a final round; the last "
    "offer stands",
    "offer_meets_last_ask": "Closed: the offer meets our last request (above target); it "
    "stands for your decision",
}

# The negotiation email a clarification goes before (no room for both in one email).
_AFTER_ANSWERS: Final[dict[str, str]] = {
    "revised_offer_after_answers": "the request for a revised offer follows the answers",
    "delivery_date_after_answers": "the delivery-date request follows the answers",
    "bafo_after_answers": "the best-and-final request follows the answers",
}

_WARNINGS: Final[dict[str, str]] = {
    "reply_by_misses_need_by": "ordering at the RFQ's reply-by date would deliver after the "
    "need-by date",
    "order_by_before_decision": "order by this date to meet the need-by date, before the "
    "negotiation ends: decide earlier or it arrives late",
    "moq_above_quantity": "minimum order quantity above the requested quantity",
    "need_by_unreachable": "the need-by date can no longer be met",
    "delivery_estimated": "delivery date estimated from an approximate lead time",
    "validity_unknown": "validity unknown",
    "validity_ends_before_decision": "the offer expires before the negotiation ends",
    "payment_terms_shorter": "payment terms shorter than the RFQ asked",
    "line_not_quoted": "not quoted",
    "order_deadline": "the supplier's offer holds only if the order reaches it by this date",
    "order_deadline_passed": "the supplier's order deadline has passed: its offer (delivery "
    "date included) no longer holds as stated",
}

# Why a supplier's reply could not be applied to its offer (the offer state's codes,
# `bido_back.quotes.state`). Shared with the Copilot's follow-up tools.
_OFFER_HOLDS: Final[dict[str, str]] = {
    "amendment_without_explicit_preservation": "a partial update that does not say the other "
    "terms are unchanged",
    "amendment_without_prior_offer": "a partial update without an earlier offer",
    "ambiguous_amendment_line_or_tier": "an update Bido cannot match to one line or tier",
    "changed_commercial_basis_requires_replacement": "a change of the offer's commercial basis "
    "(unit, price basis, minimum order, price conditions, currency, Incoterm or payment terms) "
    "that Bido does not apply on its own",
    "conflicting_update": "contradicting statements",
    "conflicting_sources": "contradicting statements",
    "supplier_clarification_required": "a reply that needs reading",
    "uncertain_message_chronology": "replies whose order is unclear",
    "untrusted_future_message_date": "a reply dated in the future",
    "cyclic_reply_relationship": "replies whose order is unclear",
    "unverified_withdrawal": "a withdrawal Bido could not verify",
    "withdrawal_for_unknown_line": "a withdrawal of a line the offer does not have",
}
# The commercial terms a held reply may change (`pos1.unit`, `currency`).
_HELD_FIELDS: Final[dict[str, str]] = {
    "unit": "unit",
    "price_basis_quantity": "price basis (quantity per price)",
    "conditional_terms": "price conditions",
    "moq": "minimum order quantity",
    "currency": "currency",
    "incoterm": "Incoterm",
    "payment_terms": "payment terms",
    "unit_price": "unit price",
    "tooling_cost": "tooling cost",
    "freight_cost": "freight cost",
    "tax_rate_percent": "tax rate",
}


def day(value: date) -> str:
    return value.isoformat()


def moment(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


def reason_text(code: str) -> str:
    """One sentence for a reason code (unknown codes are shown as they are)."""
    if code.startswith("held_by_mandate:"):
        return (
            "Held for your approval: the email is outside the negotiation mandate "
            f"({code.removeprefix('held_by_mandate:').replace('_', ' ')})"
        )
    if code.startswith("rfq_email:") and code not in _REASONS:
        return "Held for your approval: the RFQ email"
    return _REASONS.get(code, code.replace("_", " "))


def blocker_text(code: str) -> str:
    return _BLOCKERS.get(code, code.replace("_", " "))


def hold_reason_text(code: str) -> str:
    """Why a reply could not be applied, in words (unknown codes as they are)."""
    return _OFFER_HOLDS.get(code, code.replace("_", " ").replace(":", ": "))


def held_field_text(code: str) -> str:
    """`pos1.unit` -> "line 1 unit", `currency` -> "currency"."""
    where, _, name = code.partition(".")
    if name and where.startswith("pos") and where[3:].isdigit():
        return f"line {where[3:]} {_HELD_FIELDS.get(name, name.replace('_', ' '))}"
    return _HELD_FIELDS.get(code, code.replace("_", " "))


def warning_text(warning: PlanWarning) -> str:
    text = _WARNINGS.get(warning.code, warning.code.replace("_", " "))
    if warning.position is not None:
        text = f"line {warning.position}: {text}"
    if warning.on is not None:
        text += f" ({day(warning.on)})"
    return text


def _positions(lines: Iterable[LinePlan]) -> str:
    return ", ".join(str(line.position) for line in sorted(lines, key=lambda line: line.position))


def _lines_with(plan: SupplierPlan, reason: str) -> list[LinePlan]:
    return [line for line in plan.lines if reason in line.reasons]


def _details(plan: SupplierPlan) -> list[str]:
    """The specifics of a plan's reason: lines, dates, blockers."""
    parts: list[str] = []
    reason = plan.reason
    if reason in ("delivery_still_late", "delivery_need_by_unreachable", "delivery_unknown"):
        lines = _lines_with(plan, reason)
        if lines:
            earliest = [
                line.delivery.expected
                for line in lines
                if line.delivery is not None and line.delivery.expected is not None
            ]
            text = f"line(s) {_positions(lines)}"
            if earliest:
                text += f", earliest delivery {day(min(earliest))}"
            required = [
                line.delivery.required_by
                for line in lines
                if line.delivery is not None and line.delivery.required_by is not None
            ]
            if required:
                text += f", needed by {day(min(required))}"
            parts.append(text)
    elif reason in ("moq_above_quantity", "clarification_unanswered", "price_meets_last_ask"):
        lines = _lines_with(plan, reason)
        if lines:
            parts.append(f"line(s) {_positions(lines)}")
    elif reason == "offer_blocked":
        for line in plan.lines:
            if line.status == "blocked":
                parts.append(
                    f"line {line.position}: "
                    + "; ".join(dict.fromkeys(blocker_text(b) for b in line.reasons))
                )
    if reason == "offer_state_requires_review":
        if plan.hold_reasons:
            why = dict.fromkeys(hold_reason_text(code) for code in plan.hold_reasons)
            parts.append("why: " + "; ".join(why))
        if plan.held_fields:
            changed = ", ".join(held_field_text(code) for code in plan.held_fields)
            parts.append(f"the reply changes: {changed}")
    if reason == "validity_ends_before_decision" and plan.valid_until is not None:
        parts.append(f"valid until {day(plan.valid_until)}")
    if reason == "clarification_due" and plan.detail in _AFTER_ANSWERS:
        # The negotiation's own email had no room: it follows the answers.
        parts.append(_AFTER_ANSWERS[plan.detail])
    if plan.email is not None and plan.email.deferred:
        later = [line for line in plan.lines if line.rfq_line_id in plan.email.deferred]
        if later:
            parts.append(f"the price request for line(s) {_positions(later)} follows the answers")
    if reason == "email_not_prepared" and plan.detail:
        code, _, problems = plan.detail.partition(":")
        if code == "email_refused":
            # The send gate refused it (codes only): Bido never sends it as it is.
            parts.append(
                "it failed Bido's checks: " + problems.replace("_", " ").replace(",", ", ")
            )
        else:
            parts.append(plan.detail.replace("_", " "))
    return parts


def phase_label(plan: SupplierPlan, *, chases_sent: int = 0) -> str:
    """The buyer's one-line view of a supplier's negotiation."""
    text = reason_text(plan.reason)
    if plan.reason == "awaiting_quote_reminders_exhausted" and chases_sent:
        text = (
            f"Waiting for the supplier: no reply after {chases_sent} reminders (no more reminders)"
        )
    details = _details(plan)
    if details:
        text += " (" + "; ".join(details) + ")"
    if plan.until is not None:
        preposition = "cut-off" if plan.reason == "bafo_sent" else "until"
        text += f"; {preposition} {moment(plan.until)}"
    if plan.phase == "accepted_internally":
        extra: list[str] = []
        if plan.order_by is not None:
            supplier_deadline = any(
                w.code == "order_deadline" and w.on == plan.order_by for w in plan.warnings
            )
            extra.append(
                f"order by {day(plan.order_by)}, the supplier's order deadline"
                if supplier_deadline
                else f"order by {day(plan.order_by)} to meet the need-by dates"
            )
        if plan.valid_until is not None:
            extra.append(f"valid until {day(plan.valid_until)}")
        not_quoted = [line for line in plan.lines if line.status in ("not_quoted", "declined")]
        if not_quoted:
            extra.append(f"line(s) {_positions(not_quoted)} not quoted")
        if extra:
            text += "; " + "; ".join(extra)
    return text + "."


def line_reasons(line: LinePlan) -> list[str]:
    """A line's reason codes, blockers spelled out."""
    if line.status == "blocked":
        return [blocker_text(b) for b in line.reasons]
    return list(line.reasons)


def warnings_text(warnings: Sequence[PlanWarning]) -> list[str]:
    return [warning_text(w) for w in warnings]
