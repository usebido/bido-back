"""The typed input of a supplier email: what code decided, never what a model wrote.

Every field is data a builder fills from the decision (`bido_back.supplier_email.builders`)
or that another workstream feeds directly: the clarification ledger (which asks), the
negotiation strategy (which prices and dates). The renderer turns one `SupplierEmail`
into the plain-text and HTML parts; the only free text is `note`, the optional sentence
a model may write (checked by `bido_back.supplier_email.checks.note_problems`).

Values are shown exactly as stored here: quantize a price when it is decided, never
when it is shown.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final, Literal

from bido_back.supplier_email.locale import Locale

EmailType = Literal[
    "reminder",
    "clarification",
    "revised_offer",
    "delivery_date",
    "bafo",
    "not_selected",
]
EMAIL_TYPES: Final[tuple[EmailType, ...]] = (
    "reminder",
    "clarification",
    "revised_offer",
    "delivery_date",
    "bafo",
    "not_selected",
)
# Types that ask the supplier something: they carry asks, a reply-by date and the legal line.
ANSWER_REQUESTED: Final[frozenset[EmailType]] = frozenset(
    {"reminder", "clarification", "revised_offer", "delivery_date", "bafo"}
)
# At most this many numbered asks in one email (the email standard, §2.2).
MAX_ASKS: Final = 3

# What an offer may lack (the clarification's fields).
OfferField = Literal["currency", "valid_until", "unit_price", "quantity", "lead_time"]
OFFER_FIELDS: Final[tuple[OfferField, ...]] = (
    "currency",
    "valid_until",
    "unit_price",
    "quantity",
    "lead_time",
)
# The fields of the offer as a whole (not of a position).
OFFER_TERMS: Final[frozenset[OfferField]] = frozenset({"currency", "valid_until"})


@dataclass(frozen=True, slots=True)
class Money:
    """An amount in a currency, for one unit, or for `per` units when quoted per 100 or 1000."""

    amount: Decimal
    currency: str
    per: Decimal | None = None


@dataclass(frozen=True, slots=True)
class LeadTime:
    """A lead time as the supplier stated it: days, calendar or business, maybe approximate."""

    days: int
    basis: Literal["calendar", "business"] = "calendar"
    approximate: bool = False
    # "a few additional days" on top, as the supplier wrote it.
    plus_a_few_days: bool = False


# --- Facts shown under a position ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OfferedTerms:
    """Shown as "Your offer: BRL 89.00/pc · 12 business days after order" (either part optional)."""

    price: Money | None = None
    lead_time: LeadTime | None = None


@dataclass(frozen=True, slots=True)
class QuotedLeadTime:
    """Shown as "Lead time quoted: approx. 10 business days after order"."""

    lead_time: LeadTime


@dataclass(frozen=True, slots=True)
class TargetPrice:
    """Shown as "Target price: BRL 17.90/pc": the price we ask for, never the buyer's maximum."""

    price: Money


@dataclass(frozen=True, slots=True)
class RequiredDelivery:
    """Shown as "Required delivery: by 22 Oct 2026"."""

    by: date


@dataclass(frozen=True, slots=True)
class RequiredLeadTime:
    """Shown as "Required lead time: at most 10 days after order"."""

    days: int


@dataclass(frozen=True, slots=True)
class MissingValues:
    """Shown as "Missing: unit price, lead time"."""

    fields: tuple[OfferField, ...]


@dataclass(frozen=True, slots=True)
class ValuesToConfirm:
    """Shown as "To confirm: quantity the price applies to"."""

    fields: tuple[OfferField, ...]


Fact = (
    OfferedTerms
    | QuotedLeadTime
    | TargetPrice
    | RequiredDelivery
    | RequiredLeadTime
    | MissingValues
    | ValuesToConfirm
)


@dataclass(frozen=True, slots=True)
class Position:
    """One RFQ position, identified exactly as in the RFQ email, with the facts it needs here."""

    number: int
    description: str
    # The buyer's own code (with a different MPN in brackets), or the MPN; None without one.
    code: str | None
    quantities: tuple[Decimal, ...]
    unit: str
    facts: tuple[Fact, ...] = ()


# --- Asks ------------------------------------------------------------------------------

AskKind = Literal[
    # Reminder: "Can you send us your quotation? If you will not quote, please let us know."
    "quote_or_decline",
    # The offer's currency and/or validity (`fields`), to state, or to confirm.
    "offer_terms",
    # The values marked under each position (MissingValues / ValuesToConfirm facts).
    "complete_positions",
    # "Can you deliver positions 1 and 2 by 22 Oct 2026?" (RequiredDelivery facts).
    "deliver_by",
    # "Can you deliver position 2 within 10 days of the order?" (RequiredLeadTime facts).
    "meet_lead_time",
    # "Please send a revised offer for positions 1 and 2."
    "revise_price",
    # "Please send your best and final offer for ..., or confirm that your offer stands."
    "best_and_final",
]


@dataclass(frozen=True, slots=True)
class Ask:
    """One numbered question. Each is answerable with a value, a date or yes/no."""

    kind: AskKind
    positions: tuple[int, ...] = ()
    # `offer_terms` only: which offer-level fields.
    fields: tuple[OfferField, ...] = ()
    # `offer_terms` only: confirm values we read (unclear) rather than ask for missing ones.
    confirm: bool = False


# --- The email -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answered:
    """The message this email answers: our RFQ email (`request`), the supplier's first
    offer (`offer`) or a later reply (`reply`), and its date."""

    kind: Literal["request", "offer", "reply"]
    on: date


@dataclass(frozen=True, slots=True)
class ReplyBy:
    """Answer by this day; a best-and-final request also has the exact cut-off instant
    (time and zone), the same for every supplier of the RFQ."""

    day: date
    cutoff: datetime | None = None


@dataclass(frozen=True, slots=True)
class Signature:
    """The buyer who signs: printed once (a name equal to the company is not repeated)."""

    name: str
    company: str
    role: str | None = None
    phone: str | None = None


@dataclass(frozen=True, slots=True)
class SupplierEmail:
    """One email to one supplier in an RFQ thread, before rendering."""

    type: EmailType
    locale: Locale
    rfq_ref: str
    # `Re: ` plus the RFQ email's subject, byte for byte (`subject.reply_subject`).
    subject: str
    supplier_name: str
    buyer_company: str
    signature: Signature
    answered: Answered | None = None
    positions: tuple[Position, ...] = ()
    asks: tuple[Ask, ...] = ()
    reply_by: ReplyBy | None = None
    # Revised offers: the negotiation round, shown in the purpose line.
    round: int | None = None
    # The supplier may learn that its own offer is not the most competitive (never why,
    # never another supplier's name, price or terms).
    mention_competition: bool = False
    # The request concerns only some of the RFQ's positions ("no need to resend the others").
    subset: bool = False
    # A revised-offer, delivery-date or best-and-final request that also asks what the offer
    # lacks (the clarification ledger's items: one email per supplier answer), those asks
    # first. `question_positions`: the positions shown for those questions only.
    with_questions: bool = False
    question_positions: tuple[int, ...] = ()
    # The model's optional note: empty by default (see `checks.note_problems`).
    note: str = ""

    @property
    def language(self) -> str:
        return self.locale.language
