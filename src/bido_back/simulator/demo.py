"""The demo sandbox: "Motori Demo S.p.A." buying parts for an EV motor.

Used by `scripts/seed_sandbox.py` (creates it or turns it into a sandbox) and
by the sandbox api's reset (restores the personas). The tenant id, users,
supplier names, types and contacts match `scripts/seed_motor_demo.py` (the
purchase-analysis demo dataset), so both seeds build the same company in
either order.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

from bido_back.simulator.personas import Persona

TENANT_ID: Final = UUID("00000000-0000-4000-8000-0000000d0e10")
TENANT_NAME: Final = "Motori Demo S.p.A."

USERS: Final = (
    # clerk_user_id, email, name, role
    ("user_motori_buyer", "giulia.bianchi@motori-demo.example.test", "Giulia Bianchi", "member"),
    ("user_motori_approver", "marco.conti@motori-demo.example.test", "Marco Conti", "approver"),
)


# What the market charges (base currency): the latest prices of
# `seed_motor_demo.py`'s purchase history, by lower-case keyword of a line's
# description or MPN (stems, so plurals match; the longest match wins).
# Every persona prices from it, scaled by its own anchor.
REFERENCE_PRICES: Final[dict[str, str]] = {
    "lamierin": "2.38",
    "m270": "2.38",
    "alber": "18.90",
    "42crmo4": "18.90",
    "filo di rame": "11.40",
    "cul-1.2": "11.40",
    "vernic": "14.20",
    "carta isolante": "3.25",
    "nmn": "3.25",
    "magnet": "1.18",
    "ndfeb": "1.18",
    "n42sh": "1.18",
    "6205": "1.95",
    "6206": "2.62",
    "anell": "2.10",
    "carcass": "42.80",
    "scud": "13.50",
    "scudo lato comando": "14.40",
    "scudo lato opposto": "12.60",
    "resolver": "38.20",
    "connettor": "26.10",
    "termistor": "0.12",
    "ntc": "0.12",
    "viteria": "1.35",
}

PORTFOLIOS: Final[dict[str, tuple[str, ...]]] = {
    "Acciaierie Lombarde S.p.A.": ("lamierin", "m270", "acciaio elettrico", "albero", "42crmo4"),
    "Rame Italia S.r.l.": ("rame", "cul-", "vernice", "carta isolante", "nmn"),
    "Magnetica Europe GmbH": ("magnet", "ndfeb", "n42sh"),
    "Global Parts Brokers Ltd": (),  # a broker quotes anything
    "Cuscinetti Precisione S.r.l.": ("cuscinett", "6205", "6206", "tenuta"),
    "Fonderia Veneta S.p.A.": ("carcass", "scudo", "pressofus", "alsi9cu3"),
    "Resolver Tech Europe GmbH": ("resolver", "termistore", "ntc"),
    "Connettori Milano S.r.l.": ("connettor", "termistore", "ntc", "viteria"),
}


@dataclass(frozen=True, slots=True)
class DemoSupplier:
    name: str
    type: str
    country: str
    domain: str
    contact_name: str
    contact_email: str
    persona: Persona


def _p(name: str, **fields: object) -> Persona:
    return Persona.model_validate(
        {"reference_prices": REFERENCE_PRICES, "portfolio": PORTFOLIOS[name], **fields}
    )


SUPPLIERS: Final[tuple[DemoSupplier, ...]] = (
    DemoSupplier(
        "Acciaierie Lombarde S.p.A.",
        "manufacturer",
        "IT",
        "acciaierie-lombarde.example.test",
        "Paolo Ferri",
        "vendite@acciaierie-lombarde.example.test",
        _p(
            "Acciaierie Lombarde S.p.A.",
            language="it",
            behaviors=("tough_negotiator",),
            price_anchor=Decimal("1.05"),
            lead_time_days=35,
            moq="2000",
            response_delay_minutes=3,
            quote_format="pdf",
        ),
    ),
    DemoSupplier(
        "Rame Italia S.r.l.",
        "manufacturer",
        "IT",
        "rame-italia.example.test",
        "Elena Galli",
        "ordini@rame-italia.example.test",
        _p(
            "Rame Italia S.r.l.",
            language="it",
            behaviors=("cheap_but_late",),
            price_anchor=Decimal("0.85"),
            lead_time_days=30,
            response_delay_minutes=2,
            quote_format="xlsx",
        ),
    ),
    DemoSupplier(
        "Magnetica Europe GmbH",
        "manufacturer",
        "DE",
        "magnetica-europe.example.test",
        "Stefan Krause",
        "sales@magnetica-europe.example.test",
        _p(
            "Magnetica Europe GmbH",
            language="de",
            behaviors=("slow_responder",),
            lead_time_days=56,
            response_delay_minutes=10,
            quote_format="xlsx",
        ),
    ),
    DemoSupplier(
        "Global Parts Brokers Ltd",
        "reseller",
        "GB",
        "globalpartsbrokers.example.test",
        "Tom Hughes",
        "quotes@globalpartsbrokers.example.test",
        _p(
            "Global Parts Brokers Ltd",
            language="en",
            behaviors=("reseller",),
            price_anchor=Decimal("0.92"),
            lead_time_days=14,
            response_delay_minutes=1,
            quote_format="body",
        ),
    ),
    DemoSupplier(
        "Cuscinetti Precisione S.r.l.",
        "authorized_distributor",
        "IT",
        "cuscinetti-precisione.example.test",
        "Luca Moretti",
        "offerte@cuscinetti-precisione.example.test",
        _p(
            "Cuscinetti Precisione S.r.l.",
            language="it",
            lead_time_days=21,
            response_delay_minutes=2,
            quote_format="xlsx",
        ),
    ),
    DemoSupplier(
        "Fonderia Veneta S.p.A.",
        "manufacturer",
        "IT",
        "fonderia-veneta.example.test",
        "Anna Zanetti",
        "commerciale@fonderia-veneta.example.test",
        _p(
            "Fonderia Veneta S.p.A.",
            language="it",
            behaviors=("noisy",),
            lead_time_days=42,
            moq="500",
            response_delay_minutes=4,
            quote_format="body",
        ),
    ),
    DemoSupplier(
        "Resolver Tech Europe GmbH",
        "manufacturer",
        "DE",
        "resolver-tech.example.test",
        "Jana Vogel",
        "vertrieb@resolver-tech.example.test",
        _p(
            "Resolver Tech Europe GmbH",
            language="de",
            behaviors=("declines_some_lines",),
            lead_time_days=35,
            response_delay_minutes=3,
            quote_format="pdf",
        ),
    ),
    DemoSupplier(
        "Connettori Milano S.r.l.",
        "authorized_distributor",
        "IT",
        "connettori-milano.example.test",
        "Davide Riva",
        "vendite@connettori-milano.example.test",
        _p(
            "Connettori Milano S.r.l.",
            language="it",
            behaviors=("offers_alternatives",),
            lead_time_days=21,
            response_delay_minutes=2,
            quote_format="xlsx",
        ),
    ),
)

PERSONAS_BY_NAME: Final[dict[str, Persona]] = {s.name: s.persona for s in SUPPLIERS}
