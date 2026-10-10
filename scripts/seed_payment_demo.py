"""Seed a purchase order ready to be paid: the card -> stablecoin -> escrow demo.

    uv run python scripts/seed_payment_demo.py                    # the dev tenant (seed_dev.py)
    uv run python scripts/seed_payment_demo.py --payout-account   # + the supplier's bank account
    uv run python scripts/seed_payment_demo.py --tenant-id <uuid> --buyer-email <email>
    uv run python scripts/seed_payment_demo.py --currency CNY     # a PO in yuan (rates: load_fx.py)

Runs with ENVIRONMENT=local or dev (refuses prd). Uses DATABASE_URL (the app role, through
tenant-scoped transactions like the api). Each run creates a NEW order: a PO is paid once.

1. The supplier "Ningbo Precision Tooling Co., Ltd." (CN), found or created, with one contact
   (an example.test address).
2. A draft RFQ with two lines (`create_draft_rfq`), the supplier invited.
3. The RFQ email marked as sent and the supplier's quote recorded, as the worker and the
   mailbox would have: NO email goes out (demo data).
4. The award through the production path: `request_award`, the buyer's decision (an
   approver's in a four-eyes company) and the executor's `award_rfq`, which writes PO-<n>.
5. The PO marked as sent, again without emailing the supplier.
6. With --payout-account (needs PAYOUT_DETAILS_ENCRYPTION_KEY or PAYOUT_DETAILS_KMS_KEY_ID):
   the supplier's CNY bank account, with made-up demo values, sealed and recorded like the
   api does, by an approver or the owner.

Then it prints the PO and the api calls that pay it.
"""

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final, cast, get_args
from uuid import UUID

from seed_dev import TENANT_ID as DEV_TENANT_ID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.approvals import (
    ActToolRegistry,
    ApprovalExecutor,
    ApprovalForbidden,
    decide_approval,
)
from bido_back.auth import Principal, Role
from bido_back.awards import service as award_service
from bido_back.awards.schemas import AwardRequest
from bido_back.awards.send import AWARD_REJECT_HOOKS, register_award_act_tools
from bido_back.db import create_engine, tenant_session
from bido_back.db.models import (
    AuditEvent,
    Contact,
    IssuedPurchaseOrder,
    Membership,
    Message,
    Proposal,
    ProposalLine,
    Supplier,
    SupplierContact,
    User,
)
from bido_back.email.addresses import make_message_id
from bido_back.payments import service as payments_service
from bido_back.payments.bank_details import bank_details_configured, build_bank_details_cipher
from bido_back.payments.errors import PaymentInvalidState
from bido_back.payments.schemas import CreatePayoutAccountRequest
from bido_back.rfqs.audit import RFQ_STATUS_CHANGED, rfq_event
from bido_back.rfqs.mailbox import mailbox_senders_for
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest
from bido_back.rfqs.send import RfqActDeps
from bido_back.rfqs.service import NewRfqLine, add_draft_invite, create_draft_rfq
from bido_back.rfqs.suppliers import create_supplier
from bido_back.settings import Settings, get_settings
from bido_back.worker.envelope import Envelope

DEV_BUYER: Final = "user_dev_buyer"
SUPPLIER: Final = "Ningbo Precision Tooling Co., Ltd."
SUPPLIER_DOMAIN: Final = "ningbo-tooling.example.test"
CONTACT_NAME: Final = "Li Wei"
CONTACT_EMAIL: Final = f"sales@{SUPPLIER_DOMAIN}"
MANAGER_ROLES: Final = frozenset({"approver", "owner"})

# (description, part number, quantity, unit, unit price in USD, in CNY, lead time in days)
LINES: Final = (
    ("Injection mold, 4 cavities, P20 steel", "MLD-4471", Decimal(1), "pcs", "8500", "61500", 35),
    ("T1 sample housings, PA66-GF30", "HSG-4471", Decimal(200), "pcs", "2.35", "17", 40),
)
# Made-up demo values in the right shapes (CNAPS: 12 digits); not a real account.
DEMO_ACCOUNT: Final = CreatePayoutAccountRequest(
    beneficiary_name=SUPPLIER,
    bank_name="Bank of China Ningbo Branch",
    account_number="6217 8500 0000 1234 567",
    country="CN",
    currency="CNY",
    swift_bic="BKCHCNBJ92A",
    bank_code="104332000017",
    beneficiary_address="Beilun District, Ningbo, Zhejiang",
)


class _Jobs:
    """Collects the `approval.execute` job the decision enqueues; this script runs it."""

    def __init__(self) -> None:
        self.envelopes: list[Envelope] = []

    async def enqueue(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)


def _role(value: str) -> Role:
    if value not in get_args(Role):
        raise SystemExit(f"unknown membership role: {value}")
    return cast(Role, value)


async def _people(engine: AsyncEngine, tenant_id: UUID) -> list[tuple[Principal, str, str]]:
    """Every member of the company: (principal, clerk user id, email), owners first."""
    async with tenant_session(engine, tenant_id) as session:
        rows = (
            await session.execute(
                select(User.id, User.clerk_user_id, User.email, Membership.role)
                .join(Membership, Membership.user_id == User.id)
                .where(Membership.tenant_id == tenant_id)
                .order_by(Membership.role.desc(), User.email)
            )
        ).all()
    return [
        (Principal(tenant_id=tenant_id, user_id=user_id, role=_role(role)), clerk, email or "")
        for user_id, clerk, email, role in rows
    ]


def _buyer(
    people: list[tuple[Principal, str, str]], tenant_id: UUID, email: str | None
) -> tuple[Principal, str]:
    if email is not None:
        found = [(p, clerk) for p, clerk, mail in people if mail.lower() == email.lower()]
    elif tenant_id == DEV_TENANT_ID:
        found = [(p, clerk) for p, clerk, _ in people if clerk == DEV_BUYER]
    else:
        found = [(p, clerk) for p, clerk, _ in people]
    if not found:
        raise SystemExit(
            "no such member in that company: run scripts/seed_dev.py, or pass --tenant-id and "
            "--buyer-email of a member"
        )
    return found[0]


def _manager(people: list[tuple[Principal, str, str]], other_than: UUID) -> Principal | None:
    """An approver or the owner who is not `other_than` (four-eyes, payout accounts)."""
    for principal, _, _ in people:
        if principal.role in MANAGER_ROLES and principal.user_id != other_than:
            return principal
    return None


async def _supplier(engine: AsyncEngine, buyer: Principal) -> tuple[UUID, UUID]:
    """The demo supplier and its contact, found or created."""
    async with tenant_session(engine, buyer.tenant_id) as session:
        found = (
            await session.execute(
                select(Supplier.id, Contact.id)
                .join(SupplierContact, SupplierContact.supplier_id == Supplier.id)
                .join(Contact, Contact.id == SupplierContact.contact_id)
                .where(Supplier.name == SUPPLIER, Contact.email == CONTACT_EMAIL)
                .limit(1)
            )
        ).one_or_none()
    if found is not None:
        return found[0], found[1]
    created = await create_supplier(
        engine,
        buyer,
        CreateSupplierRequest(
            name=SUPPLIER,
            type="manufacturer",
            country="CN",
            language="en",
            email_domain=SUPPLIER_DOMAIN,
            contacts=[ContactIn(name=CONTACT_NAME, email=CONTACT_EMAIL)],
        ),
    )
    return created.id, created.contacts[0].id


async def _quoted_rfq(
    engine: AsyncEngine,
    settings: Settings,
    buyer: Principal,
    supplier_id: UUID,
    contact_id: UUID,
    currency: str,
    today: date,
) -> UUID:
    """An open RFQ whose email went to the supplier (marked only) and the supplier's quote."""
    now = datetime.now(UTC)
    async with tenant_session(engine, buyer.tenant_id) as session:
        rfq, lines = await create_draft_rfq(
            session,
            buyer,
            title="Housing tooling and T1 samples (payment demo)",
            lines=[
                NewRfqLine(
                    description=description,
                    part_number=part,
                    quantity=quantity,
                    unit=unit,
                    category="drawing",
                    need_by=today + timedelta(days=lead + 7),
                )
                for description, part, quantity, unit, _, _, lead in LINES
            ],
            response_due=today + timedelta(days=7),
            base_currency=currency,
            origin="api",
        )
        invite = await add_draft_invite(session, buyer, rfq, supplier_id, contact_id)
        rfq_message_id = make_message_id(settings.email_domain)
        session.add(
            Message(
                tenant_id=buyer.tenant_id,
                invite_id=invite.id,
                direction="out",
                message_id_header=rfq_message_id,
                subject=f"{rfq.ref}: request for quotation (demo, not sent)",
                occurred_at=now - timedelta(days=2),
            )
        )
        invite.thread_message_id = rfq_message_id
        invite.status = "sent"
        invite.sent_at = now - timedelta(days=2)
        rfq.status = "open"
        session.add(
            rfq_event(
                buyer.tenant_id,
                buyer.user_id,
                RFQ_STATUS_CHANGED,
                rfq.id,
                from_status="draft",
                to_status="open",
                invite_id=invite.id,
            )
        )
        reply = Message(
            tenant_id=buyer.tenant_id,
            invite_id=invite.id,
            direction="in",
            message_id_header=f"<quote-{invite.id}@{SUPPLIER_DOMAIN}>",
            in_reply_to=rfq_message_id,
            subject=f"Re: {rfq.ref}: request for quotation",
            occurred_at=now - timedelta(days=1),
        )
        session.add(reply)
        await session.flush()
        proposal = Proposal(
            tenant_id=buyer.tenant_id,
            invite_id=invite.id,
            message_id=reply.id,
            version=1,
            currency=currency,
            valid_until=today + timedelta(days=60),
            incoterm="FOB",
            payment_terms="100% in escrow, released by milestones",
        )
        session.add(proposal)
        await session.flush()
        for line, (_, _, _, unit, usd, cny, lead) in zip(lines, LINES, strict=True):
            session.add(
                ProposalLine(
                    tenant_id=buyer.tenant_id,
                    proposal_id=proposal.id,
                    rfq_line_id=line.id,
                    min_quantity=Decimal(1),
                    unit_price=Decimal(usd if currency == "USD" else cny),
                    unit=unit,
                    lead_time_days=lead,
                )
            )
        await session.flush()
        return rfq.id


async def _award(
    engine: AsyncEngine,
    settings: Settings,
    buyer: Principal,
    approver: Principal | None,
    rfq_id: UUID,
    today: date,
) -> Principal:
    """The award through the production path; returns who approved it."""
    requested = await award_service.request_award(engine, buyer, rfq_id, AwardRequest(), today)
    jobs = _Jobs()
    decider = buyer
    try:
        await decide_approval(
            engine,
            buyer,
            requested.approval_id,
            "approve",
            jobs=jobs,
            on_reject=AWARD_REJECT_HOOKS,
        )
    except ApprovalForbidden:
        # A four-eyes company: a second person decides.
        if approver is None:
            raise SystemExit("this company needs an approver other than the buyer") from None
        decider = approver
        await decide_approval(
            engine,
            approver,
            requested.approval_id,
            "approve",
            jobs=jobs,
            on_reject=AWARD_REJECT_HOOKS,
        )
    registry = ActToolRegistry()
    register_award_act_tools(
        registry,
        RfqActDeps(
            db=engine,
            email_domain=settings.email_domain,
            mailbox_senders=mailbox_senders_for(settings),
        ),
    )
    executor = ApprovalExecutor(engine, registry)
    for envelope in jobs.envelopes:
        await executor(envelope)
    return decider


async def _mark_sent(
    engine: AsyncEngine, settings: Settings, buyer: Principal, decider: Principal, rfq_id: UUID
) -> IssuedPurchaseOrder:
    """The award's PO, marked as sent to the supplier (no email goes out: demo data)."""
    async with tenant_session(engine, buyer.tenant_id) as session:
        po = await session.scalar(
            select(IssuedPurchaseOrder)
            .where(IssuedPurchaseOrder.rfq_id == rfq_id)
            .with_for_update()
        )
        if po is None:
            raise SystemExit("the award wrote no purchase order")
        po.status = "sent"
        po.sent_at = datetime.now(UTC)
        po.fulfilment_state = "sent"
        po.message_id_header = make_message_id(settings.email_domain)
        session.add(
            AuditEvent(
                tenant_id=buyer.tenant_id,
                actor_user_id=buyer.user_id,
                action="purchase_order.sent",
                payload={
                    "purchase_order_id": str(po.id),
                    "invite_id": str(po.invite_id),
                    "message_row_id": None,
                    "sender_user_id": str(buyer.user_id),
                    "approved_by": str(decider.user_id),
                    "run_id": None,
                },
            )
        )
        await session.flush()
        await session.refresh(po)
        return po


async def _payout_account(
    engine: AsyncEngine, settings: Settings, manager: Principal, supplier_id: UUID
) -> UUID:
    cipher = build_bank_details_cipher(settings)
    try:
        view = await payments_service.create_payout_account(
            engine, manager, supplier_id, DEMO_ACCOUNT, cipher
        )
        return view.id
    except PaymentInvalidState:
        accounts = await payments_service.list_payout_accounts(engine, manager, supplier_id)
        return next(a.id for a in accounts if a.status == "active")


async def seed(
    engine: AsyncEngine,
    settings: Settings,
    *,
    tenant_id: UUID,
    buyer_email: str | None,
    currency: str,
    payout_account: bool,
    today: date | None = None,
) -> None:
    day = today or date.today()
    people = await _people(engine, tenant_id)
    buyer, buyer_clerk_id = _buyer(people, tenant_id, buyer_email)
    approver = _manager(people, buyer.user_id)
    supplier_id, contact_id = await _supplier(engine, buyer)
    rfq_id = await _quoted_rfq(engine, settings, buyer, supplier_id, contact_id, currency, day)
    decider = await _award(engine, settings, buyer, approver, rfq_id, day)
    po = await _mark_sent(engine, settings, buyer, decider, rfq_id)

    print(f"purchase order {po.ref} ({po.id}): {po.currency} {po.total}, sent, ready to pay")
    print(f"  supplier {SUPPLIER} ({supplier_id})")
    account_id: UUID | None = None
    if payout_account:
        manager = buyer if buyer.role in MANAGER_ROLES else approver
        if not bank_details_configured(settings):
            print("  payout account skipped: set PAYOUT_DETAILS_ENCRYPTION_KEY (or the KMS key)")
        elif manager is None:
            print("  payout account skipped: the company has no approver or owner")
        else:
            account_id = await _payout_account(engine, settings, manager, supplier_id)
            print(f"  payout account (CNY, demo values) {account_id}")
    if currency != "USD":
        print("  the USD quote needs ECB rates: uv run python scripts/load_fx.py")

    who = f"X-Dev-User: {buyer_clerk_id}" if settings.environment == "local" else "a bearer token"
    print(f"\nPay it as the buyer ({who}):")
    print("  GET  /v1/payments/config")
    print("  POST /v1/payment-methods {token_id}   (Basis Theory Elements; test card 4242...)")
    if account_id is None:
        print(f"  POST /v1/suppliers/{supplier_id}/payout-accounts {{...}}   (approver or owner)")
    print(
        f"  POST /v1/purchase-orders/{po.id}/payment "
        f'{{"payment_method_id": "<card>", "payout_account_id": "{account_id or "<account>"}"}}'
    )
    print('  POST /v1/approvals/<approval_id>/decision {"decision": "approve"}')
    print(f"  GET  /v1/purchase-orders/{po.id}/payment")


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--tenant-id", type=UUID, default=DEV_TENANT_ID)
    parser.add_argument("--buyer-email", default=None, help="the member who buys (and pays)")
    parser.add_argument("--currency", choices=("USD", "CNY"), default="USD")
    parser.add_argument(
        "--payout-account", action="store_true", help="also record the supplier's bank account"
    )
    return parser.parse_args()


async def main() -> int:
    args = _args()
    settings = get_settings()
    if settings.environment not in ("local", "dev"):
        print("refusing: demo data only with ENVIRONMENT=local or dev", file=sys.stderr)
        return 2
    engine = create_engine(settings)
    try:
        await seed(
            engine,
            settings,
            tenant_id=args.tenant_id,
            buyer_email=args.buyer_email,
            currency=args.currency,
            payout_account=args.payout_account,
        )
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
