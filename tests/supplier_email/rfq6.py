"""RFQ-6 (the deployed thread of 2026-10-08) as email inputs: every type, in any language.

MetalNova Teste Ltda, a Brazilian supplier; the buyer "Biduuuuu" has no buyer name on file,
so name and company are the same (the signature must print once). Lines as in RFQ-6.
"""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final

from bido_back.supplier_email.builders import (
    Gap,
    Header,
    LineAsk,
    RequestedLine,
    clarification_email,
    negotiation_email,
    not_selected_email,
    position_of,
    reminder_email,
)
from bido_back.supplier_email.checks import GateContext
from bido_back.supplier_email.locale import Locale
from bido_back.supplier_email.model import (
    Answered,
    Ask,
    LeadTime,
    Money,
    QuotedLeadTime,
    ReplyBy,
    RequiredDelivery,
    Signature,
    SupplierEmail,
)

TITLE: Final = "Cotação de componentes para motor MTR-100"
# The first subject of the thread, per language (the RFQ email's own subject).
FIRST_SUBJECTS: Final[dict[str, str]] = {
    "pt": "[RFQ-6] Solicitação de cotação: Componentes para motor MTR-100",
    "en": "[RFQ-6] Request for quotation: Components for motor MTR-100",
    "de": "[RFQ-6] Angebotsanfrage: Komponenten für Motor MTR-100",
    "fr": "[RFQ-6] Demande de prix: Composants pour moteur MTR-100",
    "it": "[RFQ-6] Richiesta di offerta: Componenti per motore MTR-100",
    "es": "[RFQ-6] Solicitud de cotización: Componentes para motor MTR-100",
}
LINES: Final = (
    RequestedLine(
        1, "Rolamento motor MTR-100", "000101", None, (Decimal(70),), "pcs", date(2026, 10, 22)
    ),
    RequestedLine(
        2, "Eixo motor MTR-100", "000102", None, (Decimal(15),), "pcs", date(2026, 10, 22)
    ),
    RequestedLine(
        3, "Fio de cobre esmaltado", "000104", None, (Decimal("12.5"),), "kg", date(2026, 10, 26)
    ),
)
OFFER_DAY: Final = date(2026, 10, 8)
CUTOFF: Final = datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
OTHERS: Final = ("Fundição Paulista Ltda", "Usinagem Rio Claro S.A.")


def header(language: str, region: str | None = "BR") -> Header:
    return Header(
        rfq_ref="RFQ-6",
        subject=f"Re: {FIRST_SUBJECTS[language]}",
        locale=Locale.of(language, region),
        supplier_name="MetalNova Teste Ltda",
        buyer_company="Biduuuuu",
        signature=Signature(name="Biduuuuu", company="Biduuuuu"),
    )


def gate(language: str) -> GateContext:
    return GateContext(
        thread_language=language,
        first_subject=FIRST_SUBJECTS[language],
        rfq_title=TITLE,
        other_suppliers=OTHERS,
    )


def clarification_a(language: str, region: str | None = "BR") -> SupplierEmail:
    """§2.7 A: after reply 1, confirm delivery of positions 1 and 2 (need-by 22/10)."""
    base = clarification_email(
        header(language, region),
        answered=Answered("offer", OFFER_DAY),
        lines=LINES,
        gaps=(),
        reply_by=ReplyBy(date(2026, 10, 13)),
    )
    need = RequiredDelivery(date(2026, 10, 22))
    return replace(
        base,
        positions=(
            position_of(
                LINES[0], (QuotedLeadTime(LeadTime(10, "business", approximate=True)), need)
            ),
            position_of(
                LINES[1],
                (
                    QuotedLeadTime(
                        LeadTime(10, "business", approximate=True, plus_a_few_days=True)
                    ),
                    need,
                ),
            ),
        ),
        asks=(Ask("deliver_by", positions=(1, 2)),),
        subset=True,
    )


def clarification_missing(language: str, region: str | None = "BR") -> SupplierEmail:
    """What the clarification of today's open items renders: the validity and per-line values."""
    return clarification_email(
        header(language, region),
        answered=Answered("offer", OFFER_DAY),
        lines=LINES,
        gaps=(
            Gap(None, "valid_until", "missing"),
            Gap(1, "lead_time", "unclear"),
            Gap(2, "unit_price", "missing"),
            Gap(2, "lead_time", "missing"),
        ),
        reply_by=ReplyBy(date(2026, 10, 13)),
    )


def delivery_b(language: str, region: str | None = "BR") -> SupplierEmail:
    """§2.7 B: after reply 2, the shaft's 12 business days miss 22/10."""
    return negotiation_email(
        header(language, region),
        action="counter",
        round=1,
        answered=Answered("reply", OFFER_DAY),
        lines=LINES,
        asks=(
            LineAsk(
                position=2,
                offered=Money(Decimal("89.000000"), "BRL"),
                offered_lead=LeadTime(12, "business"),
                need_by=date(2026, 10, 22),
            ),
        ),
        mention_competition=False,
        reply_by=ReplyBy(date(2026, 10, 14)),
    )


def revised_offer(language: str, region: str | None = "BR") -> SupplierEmail:
    """A price counter on positions 1 and 3 (hypothetical targets), competition mentioned."""
    return negotiation_email(
        header(language, region),
        action="counter",
        round=1,
        answered=Answered("reply", OFFER_DAY),
        lines=LINES,
        asks=(
            LineAsk(
                position=1,
                offered=Money(Decimal("19.50"), "BRL"),
                target=Money(Decimal("17.90"), "BRL"),
            ),
            LineAsk(
                position=3,
                offered=Money(Decimal("95.00"), "BRL"),
                target=Money(Decimal("90"), "BRL"),
            ),
        ),
        mention_competition=True,
        reply_by=ReplyBy(date(2026, 10, 14)),
    )


def bafo(language: str, region: str | None = "BR") -> SupplierEmail:
    """The best and final request: one cut-off with time and zone for every supplier."""
    return negotiation_email(
        header(language, region),
        action="bafo",
        round=2,
        answered=Answered("reply", date(2026, 10, 14)),
        lines=LINES,
        asks=(
            LineAsk(
                position=1,
                offered=Money(Decimal("18.40"), "BRL"),
                target=Money(Decimal("17.90"), "BRL"),
            ),
            LineAsk(
                position=3,
                offered=Money(Decimal("92.00"), "BRL"),
                target=Money(Decimal("90"), "BRL"),
            ),
        ),
        mention_competition=False,
        reply_by=ReplyBy(CUTOFF.date(), cutoff=CUTOFF),
    )


def reminder(language: str, region: str | None = "BR") -> SupplierEmail:
    return reminder_email(
        header(language, region),
        requested_on=OFFER_DAY,
        reply_by=ReplyBy(date(2026, 10, 20)),
    )


def not_selected(language: str, region: str | None = "BR") -> SupplierEmail:
    return not_selected_email(header(language, region), answered=Answered("offer", OFFER_DAY))


SAMPLES: Final = {
    "01-reminder": reminder,
    "02-clarification-delivery": clarification_a,
    "03-clarification-missing": clarification_missing,
    "04-revised-offer": revised_offer,
    "05-delivery-date": delivery_b,
    "06-bafo": bafo,
    "07-not-selected": not_selected,
}
