"""Seed the "Motori Demo S.p.A." demo tenant: an EV motor, its BOM, stock and history.

    uv run python scripts/seed_motor_demo.py                  # seed (idempotent)
    uv run python scripts/seed_motor_demo.py --write-fixtures # also refresh the xlsx files

LOCAL ONLY: refuses unless ENVIRONMENT=local. Uses DATABASE_URL (the app role,
through tenant_session, like the api).

What it creates (dates are relative to the day it runs, so the demo stays
fresh):

* tenant "Motori Demo S.p.A." (fixed id, re-runs find it), a buyer
  (`X-Dev-User: user_motori_buyer`) and an approver (`user_motori_approver`);
* 8 suppliers with one contact each (example.test addresses);
* the product "Motor EV-120" (a 120 kW traction motor) with a 16-part BOM;
* stock and open orders, 12 months of purchase history (prices in EUR, a
  copper price increase, late castings, a rejected magnet lot from the broker,
  a few rejected seals), and a production plan of 400 motors next month.

The data goes through the same spreadsheet importer the api uses
(`bido_back.purchasing.service.import_rows`), built as xlsx in memory.
`--write-fixtures` also writes those workbooks to scripts/demo_data/, so they
can be opened, edited and imported by hand (POST /v1/onboarding/uploads, then
POST /v1/data/imports).

Then ask the copilot (as user_motori_buyer): "faz uma análise de motor".
"""

import argparse
import asyncio
import io
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bido_back.auth import Principal
from bido_back.db import create_engine, tenant_session
from bido_back.db.models import Contact, Membership, Supplier, SupplierContact, Tenant, User
from bido_back.purchasing.importers import ImportType, read_table
from bido_back.purchasing.service import import_rows
from bido_back.settings import get_settings

TENANT_ID: Final = UUID("00000000-0000-4000-8000-0000000d0e10")
TENANT_NAME: Final = "Motori Demo S.p.A."
PRODUCT: Final = "Motor EV-120"
FIXTURES: Final = Path(__file__).parent / "demo_data"

USERS: Final = (
    # clerk_user_id, email, name, role
    ("user_motori_buyer", "giulia.bianchi@motori-demo.example.test", "Giulia Bianchi", "member"),
    ("user_motori_approver", "marco.conti@motori-demo.example.test", "Marco Conti", "approver"),
)

SUPPLIERS: Final = (
    # name, type, country, language, domain, contact name, contact email
    ("Acciaierie Lombarde S.p.A.", "manufacturer", "IT", "it", "acciaierie-lombarde",
     "Paolo Ferri", "vendite"),
    ("Rame Italia S.r.l.", "manufacturer", "IT", "it", "rame-italia", "Elena Galli", "ordini"),
    ("Magnetica Europe GmbH", "manufacturer", "DE", "de", "magnetica-europe",
     "Stefan Krause", "sales"),
    ("Global Parts Brokers Ltd", "reseller", "GB", "en", "globalpartsbrokers",
     "Tom Hughes", "quotes"),
    ("Cuscinetti Precisione S.r.l.", "authorized_distributor", "IT", "it",
     "cuscinetti-precisione", "Luca Moretti", "offerte"),
    ("Fonderia Veneta S.p.A.", "manufacturer", "IT", "it", "fonderia-veneta",
     "Anna Zanetti", "commerciale"),
    ("Resolver Tech Europe GmbH", "manufacturer", "DE", "de", "resolver-tech",
     "Jana Vogel", "vertrieb"),
    ("Connettori Milano S.r.l.", "authorized_distributor", "IT", "it", "connettori-milano",
     "Davide Riva", "vendite"),
)  # fmt: skip

# part number, description, MPN, manufacturer, category, unit, quantity per motor
ITEMS: Final = (
    ("LAM-M270", "Lamierini acciaio elettrico M270-35A (statore + rotore)", "M270-35A",
     "Acciaierie Lombarde", "material", "kg", 18.5),
    ("CU-WIRE-12", "Filo di rame smaltato 1,2 mm classe H", "CuL-1.2-H", "Rame Italia",
     "material", "kg", 6.2),
    ("MAG-N42SH", "Magnete NdFeB N42SH 40x10x5 mm", "N42SH-40105", "Magnetica Europe",
     "magnets", "pz", 32),
    ("BRG-6205", "Cuscinetto 6205-2RS C3", "6205-2RSH/C3", "SKF", "bearings", "pz", 1),
    ("BRG-6206", "Cuscinetto 6206-2RS C3", "6206-2RSH/C3", "SKF", "bearings", "pz", 1),
    ("SHF-120", "Albero rotore 42CrMo4 lavorato", None, None, "machined", "pz", 1),
    ("HSG-AL-120", "Carcassa alluminio AlSi9Cu3 pressofusa", None, None, "casting", "pz", 1),
    ("ESH-DE", "Scudo lato comando (DE)", None, None, "casting", "pz", 1),
    ("ESH-NDE", "Scudo lato opposto (NDE)", None, None, "casting", "pz", 1),
    ("RSV-12", "Resolver 12 poli", "RT-12P-60", "Resolver Tech", "sensors", "pz", 1),
    ("NTC-10K", "Termistore NTC 10k AEC-Q200", "NCP18XH103F03RB", "Murata", "sensors", "pz", 2),
    ("CON-HV3", "Connettore HV trifase 400 A", "HVC-3P-400", "TE Connectivity", "connectors",
     "pz", 1),
    ("VRN-H", "Vernice di impregnazione classe H", "VI-H200", "Elantas", "material", "kg", 0.8),
    ("INS-NMN", "Carta isolante NMN classe H", "NMN-0.25", "DuPont", "material", "m", 1.6),
    ("FST-M6", "Kit viteria M6 A2-70", None, None, "fasteners", "pz", 1),
    ("SEAL-45", "Anello di tenuta 45x62x8 FKM", "BAUMSLX7-45628", "Freudenberg", "seals", "pz", 1),
)  # fmt: skip

# part number -> (on hand, on order)
STOCK: Final = {
    "LAM-M270": (1800, 2000), "CU-WIRE-12": (420, 0), "MAG-N42SH": (2400, 4000),
    "BRG-6205": (120, 200), "BRG-6206": (90, 200), "SHF-120": (60, 0), "HSG-AL-120": (35, 100),
    "ESH-DE": (80, 150), "ESH-NDE": (450, 0), "RSV-12": (40, 0), "NTC-10K": (300, 0),
    "CON-HV3": (25, 120), "VRN-H": (95, 0), "INS-NMN": (210, 0), "FST-M6": (600, 0),
    "SEAL-45": (150, 0),
}  # fmt: skip

LAG: Final = {  # supplier -> days late on some orders (0 = on time)
    "Fonderia Veneta S.p.A.": (9, 0, 14, 6),
    "Cuscinetti Precisione S.r.l.": (0, 0, 5, 0),
}

# supplier, part number, months ago, quantity, unit price, lead days, rejected
HISTORY: Final = (
    ("Acciaierie Lombarde S.p.A.", "LAM-M270", 11, 6000, 2.31, 35, 0),
    ("Acciaierie Lombarde S.p.A.", "LAM-M270", 7, 6500, 2.35, 35, 0),
    ("Acciaierie Lombarde S.p.A.", "LAM-M270", 3, 6000, 2.38, 35, 0),
    ("Acciaierie Lombarde S.p.A.", "SHF-120", 10, 350, 18.20, 28, 0),
    ("Acciaierie Lombarde S.p.A.", "SHF-120", 5, 350, 18.50, 28, 0),
    ("Acciaierie Lombarde S.p.A.", "SHF-120", 1, 300, 18.90, 28, 0),
    ("Rame Italia S.r.l.", "CU-WIRE-12", 11, 2000, 9.80, 21, 0),
    ("Rame Italia S.r.l.", "CU-WIRE-12", 8, 2000, 10.10, 21, 0),
    ("Rame Italia S.r.l.", "CU-WIRE-12", 4, 2200, 10.90, 21, 0),
    ("Rame Italia S.r.l.", "CU-WIRE-12", 1, 2000, 11.40, 21, 0),
    ("Rame Italia S.r.l.", "VRN-H", 9, 300, 13.80, 14, 0),
    ("Rame Italia S.r.l.", "VRN-H", 3, 300, 14.20, 14, 0),
    ("Rame Italia S.r.l.", "INS-NMN", 9, 600, 3.10, 14, 0),
    ("Rame Italia S.r.l.", "INS-NMN", 3, 600, 3.25, 14, 0),
    ("Magnetica Europe GmbH", "MAG-N42SH", 11, 10000, 1.12, 56, 0),
    ("Magnetica Europe GmbH", "MAG-N42SH", 6, 10000, 1.15, 56, 0),
    ("Magnetica Europe GmbH", "MAG-N42SH", 2, 12000, 1.18, 56, 0),
    ("Global Parts Brokers Ltd", "MAG-N42SH", 4, 2000, 0.96, 10, 300),
    ("Global Parts Brokers Ltd", "BRG-6205", 6, 200, 1.70, 7, 0),
    ("Global Parts Brokers Ltd", "NTC-10K", 5, 1000, 0.09, 7, 0),
    ("Cuscinetti Precisione S.r.l.", "BRG-6205", 10, 400, 1.88, 14, 0),
    ("Cuscinetti Precisione S.r.l.", "BRG-6205", 6, 400, 1.92, 14, 0),
    ("Cuscinetti Precisione S.r.l.", "BRG-6205", 2, 350, 1.95, 14, 0),
    ("Cuscinetti Precisione S.r.l.", "BRG-6206", 10, 400, 2.55, 14, 0),
    ("Cuscinetti Precisione S.r.l.", "BRG-6206", 2, 350, 2.62, 14, 0),
    ("Cuscinetti Precisione S.r.l.", "SEAL-45", 8, 500, 2.05, 14, 40),
    ("Cuscinetti Precisione S.r.l.", "SEAL-45", 3, 400, 2.10, 14, 0),
    ("Fonderia Veneta S.p.A.", "HSG-AL-120", 11, 350, 41.50, 42, 0),
    ("Fonderia Veneta S.p.A.", "HSG-AL-120", 7, 350, 42.00, 42, 0),
    ("Fonderia Veneta S.p.A.", "HSG-AL-120", 3, 300, 42.80, 42, 0),
    ("Fonderia Veneta S.p.A.", "ESH-DE", 9, 350, 14.10, 42, 0),
    ("Fonderia Veneta S.p.A.", "ESH-DE", 3, 300, 14.40, 42, 0),
    ("Fonderia Veneta S.p.A.", "ESH-NDE", 9, 350, 12.40, 42, 0),
    ("Fonderia Veneta S.p.A.", "ESH-NDE", 3, 300, 12.60, 42, 0),
    ("Resolver Tech Europe GmbH", "RSV-12", 9, 350, 37.50, 49, 0),
    ("Resolver Tech Europe GmbH", "RSV-12", 3, 300, 38.20, 49, 0),
    ("Connettori Milano S.r.l.", "CON-HV3", 8, 350, 25.40, 21, 0),
    ("Connettori Milano S.r.l.", "CON-HV3", 2, 300, 26.10, 21, 0),
    ("Connettori Milano S.r.l.", "NTC-10K", 8, 2000, 0.12, 14, 0),
    ("Connettori Milano S.r.l.", "NTC-10K", 2, 2000, 0.12, 14, 0),
    ("Connettori Milano S.r.l.", "FST-M6", 6, 800, 1.35, 10, 0),
)  # fmt: skip


SUPPLIER_INDEX: Final = {supplier[0]: i for i, supplier in enumerate(SUPPLIERS, start=1)}


def _months_ago(today: date, months: int) -> date:
    return today - timedelta(days=30 * months + 3)


def workbooks(today: date) -> dict[ImportType, list[list[object]]]:
    """The demo data as spreadsheet rows (Italian headers, like a real export)."""
    bom: list[list[object]] = [
        ["Prodotto", "Codice Articolo", "Descrizione", "Codice Produttore", "Produttore",
         "Categoria", "Unità", "Quantità per unità"],
    ]  # fmt: skip
    for number, desc, mpn, maker, category, unit, per_unit in ITEMS:
        bom.append([PRODUCT, number, desc, mpn, maker, category, unit, per_unit])

    stock: list[list[object]] = [["Codice Articolo", "Giacenza", "In ordine", "Magazzino"]]
    for number, (on_hand, on_order) in STOCK.items():
        stock.append([number, on_hand, on_order, "Torino"])

    history: list[list[object]] = [
        ["Numero Ordine", "Fornitore", "Codice Articolo", "Quantità", "Prezzo Unitario",
         "Valuta", "Data Ordine", "Data Consegna Prevista", "Data Ricevimento", "Scarti"],
    ]  # fmt: skip
    seen: dict[str, int] = {}
    for supplier, number, months, quantity, price, lead, rejected in HISTORY:
        ordered = _months_ago(today, months)
        promised = ordered + timedelta(days=lead)
        nth = seen.get(supplier, 0)
        seen[supplier] = nth + 1
        lags = LAG.get(supplier, (0,))
        received = promised + timedelta(days=lags[nth % len(lags)])
        po = f"PO-{ordered:%y%m}-{SUPPLIER_INDEX[supplier]:02d}{nth + 1:02d}"
        history.append(
            [po, supplier, number, quantity, price, "EUR", ordered, promised,
             received if received <= today else None, rejected]
        )  # fmt: skip

    first_of_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    plan: list[list[object]] = [
        ["Prodotto", "Quantità", "Data"],
        [PRODUCT, 400, first_of_next + timedelta(days=14)],
    ]
    return {"bom": bom, "inventory": stock, "purchase_history": history, "production_plan": plan}


def to_xlsx(rows: list[list[object]]) -> bytes:
    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]

    workbook = cast(Any, Workbook())
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


async def _user(session: AsyncSession, clerk_id: str, email: str, name: str, role: str) -> UUID:
    user = await session.scalar(select(User).where(User.clerk_user_id == clerk_id))
    if user is None:
        user = User(clerk_user_id=clerk_id, email=email)
        session.add(user)
        await session.flush()
    membership = await session.scalar(
        select(Membership).where(Membership.tenant_id == TENANT_ID, Membership.user_id == user.id)
    )
    if membership is None:
        session.add(Membership(tenant_id=TENANT_ID, user_id=user.id, role=role))
    if await session.scalar(select(Contact).where(Contact.email == email)) is None:
        session.add(Contact(tenant_id=TENANT_ID, kind="buyer", name=name, email=email))
    await session.flush()
    return user.id


async def _supplier(
    session: AsyncSession,
    name: str,
    kind: str,
    country: str,
    language: str,
    domain: str,
    contact_name: str,
    mailbox: str,
) -> None:
    supplier = await session.scalar(select(Supplier).where(Supplier.name == name))
    if supplier is not None:
        return
    supplier = Supplier(
        tenant_id=TENANT_ID,
        name=name,
        type=kind,
        country=country,
        language=language,
        email_domain=f"{domain}.example.test",
    )
    session.add(supplier)
    await session.flush()
    contact = Contact(
        tenant_id=TENANT_ID,
        kind="supplier",
        name=contact_name,
        email=f"{mailbox}@{domain}.example.test",
    )
    session.add(contact)
    await session.flush()
    session.add(
        SupplierContact(tenant_id=TENANT_ID, supplier_id=supplier.id, contact_id=contact.id)
    )
    await session.flush()


async def seed(engine: AsyncEngine, today: date | None = None) -> list[UUID]:
    """Create or refresh the demo tenant, its users, suppliers and data; return the user ids.

    Idempotent. No environment check: callers decide (main() is local only;
    scripts/link_demo_org.py also allows dev).
    """
    books = workbooks(today or date.today())
    async with tenant_session(engine, TENANT_ID) as session:
        if await session.get(Tenant, TENANT_ID) is None:
            session.add(Tenant(id=TENANT_ID, name=TENANT_NAME))
            await session.flush()
        users = [await _user(session, *user) for user in USERS]
        for supplier in SUPPLIERS:
            await _supplier(session, *supplier)
    principal = Principal(tenant_id=TENANT_ID, user_id=users[0])
    for kind, rows in books.items():
        report = await import_rows(
            engine, principal, kind, read_table(to_xlsx(rows), f"{kind}.xlsx")
        )
        failed = f", {report.rows_failed} failed" if report.rows_failed else ""
        print(f"import    {kind:<17} {report.rows_total} rows{failed}")
        for error in report.errors:
            print(f"  row {error.row} {error.field}: {error.message}", file=sys.stderr)
    return users


async def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the Motori Demo tenant (local only).")
    parser.add_argument("--write-fixtures", action="store_true", help="refresh demo_data/*.xlsx")
    args = parser.parse_args()

    settings = get_settings()
    if settings.environment != "local":
        print("seed_motor_demo.py runs only with ENVIRONMENT=local", file=sys.stderr)
        return 1
    if args.write_fixtures:
        FIXTURES.mkdir(exist_ok=True)
        for kind, rows in workbooks(date.today()).items():
            (FIXTURES / f"motor_ev120_{kind}.xlsx").write_bytes(to_xlsx(rows))

    engine = create_engine(settings)
    try:
        users = await seed(engine)
    finally:
        await engine.dispose()

    print(f"tenant    {TENANT_ID}  {TENANT_NAME}")
    for (clerk_id, _, _, role), user_id in zip(USERS, users, strict=True):
        print(f"user      {user_id}  {role:<8}  X-Dev-User: {clerk_id}")
    print('try       POST /v1/analyses {"product": "motor"}  or ask the copilot: '
          '"faz uma análise de motor"')  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
