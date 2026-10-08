"""Scripted model outputs for mock mode: what a correct extraction returns.

Only the cases whose values all sit in the e-mail body are scripted (sources
must quote real text). The scripted outputs go through the real validation,
so mock mode still exercises grounding, parsing and the harness checks.
"""

from datetime import date, timedelta
from typing import Final

from bido_back.agents.catalog.contracts import (
    ExtractedValue,
    ExtraOfferDraft,
    ProposalLineDraft,
    QuoteExtractionInput,
    QuoteExtractionOutput,
    SourceRef,
)
from bido_back.agents.context import ToolContext
from evals.harness import Extractor

_NONE: Final = ExtractedValue(value=None, source=None, confidence=0)


def _ev(value: str, quote: str) -> ExtractedValue:
    return ExtractedValue(
        value=value, source=SourceRef(kind="email_body", quote=quote), confidence=0.95
    )


def _line(position: int, mpn: str, qty: tuple[str, str], price: tuple[str, str],
          unit: tuple[str, str], lead: tuple[str, str]) -> ProposalLineDraft:  # fmt: skip
    return ProposalLineDraft(
        rfq_line_position=position,
        offered_mpn=_ev(mpn, mpn),
        min_quantity=_ev(*qty),
        unit_price=_ev(*price),
        unit=_ev(*unit),
        tooling_cost=_NONE,
        moq=_NONE,
        lead_time_days=_ev(*lead),
        origin_country=_NONE,
        qualification=_NONE,
    )


def scripted_outputs(today: date) -> dict[str, QuoteExtractionOutput]:
    valid = today + timedelta(days=60)
    return {
        "it_body_prices": QuoteExtractionOutput(
            is_quote=True,
            currency=_ev("EUR", "0,039 EUR"),
            valid_until=_ev(valid.isoformat(), valid.strftime("%d/%m/%Y")),
            incoterm=_ev("DAP", "Resa DAP Leipzig"),
            payment_terms=_ev("60 giorni d.f.", "Pagamento 60 giorni d.f."),
            lines=[
                _line(
                    1,
                    "NTC-10K-3950",
                    ("10000", "10.000 pz"),
                    ("0.039", "0,039 EUR"),
                    ("pz", "pz"),
                    ("35", "5 settimane"),
                ),
                _line(
                    2,
                    "TP-2-300",
                    ("400", "400 pz"),
                    ("7.90", "7,90 EUR"),
                    ("pz", "pz"),
                    ("21", "3 settimane"),
                ),
            ],  # fmt: skip
        ),
        "en_noisy_other_divisions": QuoteExtractionOutput(
            is_quote=True,
            currency=_ev("EUR", "EUR 0.041"),
            valid_until=_ev(valid.isoformat(), valid.isoformat()),
            incoterm=_ev("FCA", "FCA Milan"),
            payment_terms=_ev("30 days net", "payment 30 days net"),
            lines=[
                _line(
                    1,
                    "NTC-10K-3950",
                    ("10000", "10,000 pcs"),
                    ("0.041", "EUR 0.041"),
                    ("pcs", "pcs"),
                    ("30", "lead time 30 days"),
                ),
                _line(
                    2,
                    "TP-2-300",
                    ("400", "400 pcs"),
                    ("7.70", "EUR 7.70"),
                    ("pcs", "pcs"),
                    ("20", "lead time 20 days"),
                ),
            ],  # fmt: skip
            extra_offers=[
                ExtraOfferDraft(
                    description="Servo drive SD-200 (Industrial Drives division)",
                    mpn="SD-200",
                    quantity="min. 10 units",
                    unit_price="199",
                    currency="EUR",
                    conditions="special this month",
                    quote="servo drive SD-200 at EUR 199 per unit (min. 10 units)",
                )
            ],
        ),
        "en_decline": QuoteExtractionOutput(
            is_quote=False,
            declined=True,
            currency=_NONE,
            valid_until=_NONE,
            incoterm=_NONE,
            payment_terms=_NONE,
        ),
    }


def mock_extractor(today: date) -> Extractor:
    outputs = scripted_outputs(today)

    async def extract(task: QuoteExtractionInput, ctx: ToolContext) -> QuoteExtractionOutput | None:
        for name, output in outputs.items():
            if _matches(name, task):
                return output
        return None

    return extract


def _matches(name: str, task: QuoteExtractionInput) -> bool:
    body = task.email_body
    return {
        "it_body_prices": "Buongiorno" in body,
        "en_noisy_other_divisions": "Industrial Drives" in body,
        "en_decline": "cannot quote" in body,
    }[name]
