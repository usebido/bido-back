"""The factory's link to one payment: no login, a random token, only its sha256 kept.

    buyer:   POST   /v1/purchase-orders/{id}/payment/factory-link ──► {token, url}, once
             DELETE /v1/purchase-orders/{id}/payment/factory-link ──► revoked
    factory: every /v1/factory/... request carries the token in `X-Factory-Token`

* The token is 256 random bits (`secrets.token_urlsafe(32)`). Bido stores its sha256 only,
  shows the token once, and puts it in the page URL's fragment
  (`<FACTORY_PAGE_URL>#<token>`), which browsers never send to a server.
* One active link per payment, for a funded payment: issuing one revokes
  the previous one in the same transaction. A link expires after
  `factory_link_ttl_days`. Unknown, revoked and expired tokens get the same answer
  (`FactoryLinkInvalid`, HTTP 401), so a token cannot be probed.
* Resolving a token: its format is checked first, so junk never reaches the database;
  then `factory_link_tenant(sha256)` (SECURITY DEFINER, migration 0043) names the
  tenant and nothing else, and the link is read again in that tenant's session.
  Reading changes nothing but `last_used_at`, at most once a minute.
* The factory confirms the order once (`accepted_at`) before its first upload; a new
  link must be confirmed again.
* Logs and audit events carry ids only, never the token.

Also the row helpers the other modules share: a PO's current payment, and the payment
row locked (lock the payment before its milestones).
"""

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import IssuedPurchaseOrder, PoFactoryLink, PoPayment
from bido_back.payments.errors import FactoryLinkInvalid, PaymentInvalidState, PaymentNotFound
from bido_back.payments.milestones.proposals import FACTORY_LINK_ISSUED, FACTORY_LINK_REVOKED
from bido_back.payments.milestones.views import FactoryAcceptedView, FactoryLinkView
from bido_back.rfqs.audit import rfq_event
from bido_back.settings import Settings

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

TOKEN_BYTES: Final = 32
# `secrets.token_urlsafe(32)`: 43 characters of base64url, no padding.
_TOKEN: Final = re.compile(r"[A-Za-z0-9_-]{43}")
# How often a link's `last_used_at` is written at most (the page polls).
LAST_USED_EVERY: Final = timedelta(minutes=1)
# The factory confirmed its order (`po_factory_links.accepted_at`); keyed by `rfq_id`.
FACTORY_ORDER_ACCEPTED: Final = "purchase_order.factory_order_accepted"
# A payment whose escrow holds (or held) the money: the factory may get a link.
LINKABLE_STATES: Final = ("funded",)
# A payment that ended before any money moved (`payments.service`).
_ENDED_STATES: Final = ("rejected", "expired", "declined")
# The one answer for every bad token.
INVALID_LINK: Final = "this factory link is not valid"
_TENANT_OF_LINK = text("SELECT factory_link_tenant(:token_hash)")


@dataclass(frozen=True, slots=True)
class FactoryAccess:
    """Who the factory is on this request: its link, the payment and the tenant it names."""

    tenant_id: UUID
    link_id: UUID
    payment_id: UUID
    expires_at: datetime


def new_factory_token() -> str:
    """A new link token: 256 random bits, URL-safe."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def factory_token_hash(token: str) -> str:
    """What Bido stores of a token: its sha256, lowercase hex."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- Shared row helpers ------------------------------------------------------------------------


def _is_live(payment: PoPayment) -> bool:
    if payment.state in _ENDED_STATES:
        return False
    return not (payment.state == "failed" and payment.charged_at is None)


async def purchase_order_payment(session: AsyncSession, po_id: UUID) -> PoPayment:
    """The PO's live payment, else its latest one (what `GET .../payment` shows).

    `PaymentNotFound` for an unknown PO (another tenant's looks the same) or one never paid.
    """
    found = await session.scalar(
        select(IssuedPurchaseOrder.id).where(IssuedPurchaseOrder.id == po_id)
    )
    if found is None:
        raise PaymentNotFound(f"purchase order {po_id} not found")
    payments = list(
        await session.scalars(
            select(PoPayment)
            .where(PoPayment.purchase_order_id == po_id)
            .order_by(PoPayment.created_at.desc(), PoPayment.id)
        )
    )
    if not payments:
        raise PaymentNotFound(f"purchase order {po_id} has no payment")
    return next((p for p in payments if _is_live(p)), payments[0])


async def locked_payment(session: AsyncSession, payment_id: UUID) -> PoPayment:
    """The payment row locked `FOR UPDATE` and read fresh. Lock it before its milestones."""
    payment = await session.scalar(
        select(PoPayment)
        .where(PoPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if payment is None:
        raise PaymentNotFound(f"payment {payment_id} not found")
    return payment


async def active_link(
    session: AsyncSession, access: FactoryAccess, *, lock: bool = False
) -> PoFactoryLink:
    """The factory's link, still active and unexpired, read again in the transaction
    (a link revoked since the request began no longer acts); `FactoryLinkInvalid` else."""
    query = select(PoFactoryLink).where(
        PoFactoryLink.id == access.link_id,
        PoFactoryLink.payment_id == access.payment_id,
        PoFactoryLink.revoked_at.is_(None),
        PoFactoryLink.expires_at > func.now(),
    )
    if lock:
        query = query.with_for_update()
    link = await session.scalar(query.execution_options(populate_existing=True))
    if link is None:
        raise FactoryLinkInvalid(INVALID_LINK)
    return link


async def _order(session: AsyncSession, payment: PoPayment) -> IssuedPurchaseOrder:
    po = await session.get(IssuedPurchaseOrder, payment.purchase_order_id)
    if po is None:
        raise PaymentNotFound(f"payment {payment.id}: its purchase order is gone")
    return po


async def _revoke_active(session: AsyncSession, payment_id: UUID, user_id: UUID) -> list[UUID]:
    """Revoke the payment's active link (at most one); the ids revoked."""
    revoked = await session.scalars(
        update(PoFactoryLink)
        .where(PoFactoryLink.payment_id == payment_id, PoFactoryLink.revoked_at.is_(None))
        .values(revoked_at=func.now(), revoked_by=user_id)
        .returning(PoFactoryLink.id)
        .execution_options(synchronize_session=False)
    )
    return list(revoked)


# --- Issuing and revoking (the buyer) ----------------------------------------------------------


async def issue_factory_link(
    db: Db,
    principal: Principal,
    po_id: UUID,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> FactoryLinkView:
    """A new link for the PO's funded payment (any member); the previous one stops working.

    The token is in the answer only: Bido keeps its hash. `PaymentNotFound` for an unknown
    PO or one never paid, `PaymentInvalidState` while its payment is not funded.
    """
    issued_at = now or datetime.now(UTC)
    token = new_factory_token()
    async with tenant_session(db, principal.tenant_id) as session:
        current = await purchase_order_payment(session, po_id)
        payment = await locked_payment(session, current.id)
        if payment.state not in LINKABLE_STATES:
            raise PaymentInvalidState(
                f"payment {payment.id} is {payment.state}: the factory gets its link once "
                "the order's payment is funded"
            )
        replaced = await _revoke_active(session, payment.id, principal.user_id)
        link = PoFactoryLink(
            tenant_id=principal.tenant_id,
            payment_id=payment.id,
            token_hash=factory_token_hash(token),
            created_by=principal.user_id,
            created_at=issued_at,
            expires_at=issued_at + timedelta(days=settings.factory_link_ttl_days),
        )
        session.add(link)
        await session.flush()
        po = await _order(session, payment)
        session.add(
            rfq_event(
                principal.tenant_id,
                principal.user_id,
                FACTORY_LINK_ISSUED,
                po.rfq_id,
                payment_id=payment.id,
                link_id=link.id,
                replaced_link_id=replaced[0] if replaced else None,
                purchase_order_id=po.id,
                purchase_order_ref=po.ref,
                expires_at=link.expires_at,
            )
        )
        await session.flush()
        link_id, payment_id, expires_at = link.id, payment.id, link.expires_at
    logger.info(
        "factory link issued tenant=%s payment=%s link=%s replaced=%d",
        principal.tenant_id,
        payment_id,
        link_id,
        len(replaced),
    )
    url = f"{settings.factory_page_url}#{token}" if settings.factory_page_url else None
    return FactoryLinkView(token=token, url=url, expires_at=expires_at)


async def revoke_factory_link(db: Db, principal: Principal, po_id: UUID) -> None:
    """Stop the PO payment's factory link now (any member). Nothing to revoke is fine."""
    async with tenant_session(db, principal.tenant_id) as session:
        current = await purchase_order_payment(session, po_id)
        payment = await locked_payment(session, current.id)
        revoked = await _revoke_active(session, payment.id, principal.user_id)
        if not revoked:
            return
        po = await _order(session, payment)
        for link_id in revoked:
            session.add(
                rfq_event(
                    principal.tenant_id,
                    principal.user_id,
                    FACTORY_LINK_REVOKED,
                    po.rfq_id,
                    payment_id=payment.id,
                    link_id=link_id,
                    purchase_order_id=po.id,
                    purchase_order_ref=po.ref,
                )
            )
        await session.flush()
        payment_id = payment.id
    logger.info(
        "factory link revoked tenant=%s payment=%s links=%d",
        principal.tenant_id,
        payment_id,
        len(revoked),
    )


# --- The factory's side ------------------------------------------------------------------------


async def _link_tenant(db: Db, token_hash: str) -> UUID | None:
    """The tenant of the active link with this hash (tenant id only), outside any tenant."""
    sessionmaker = db if isinstance(db, async_sessionmaker) else create_sessionmaker(db)
    async with sessionmaker() as session, session.begin():
        found: object = await session.scalar(_TENANT_OF_LINK, {"token_hash": token_hash})
    return found if isinstance(found, UUID) else None


async def resolve_factory_link(db: Db, token: str | None) -> FactoryAccess:
    """The factory's access from its token, or `FactoryLinkInvalid` (one answer for a
    missing, malformed, unknown, revoked or expired token)."""
    if token is None or _TOKEN.fullmatch(token) is None:
        raise FactoryLinkInvalid(INVALID_LINK)
    token_hash = factory_token_hash(token)
    tenant_id = await _link_tenant(db, token_hash)
    if tenant_id is None:
        raise FactoryLinkInvalid(INVALID_LINK)
    async with tenant_session(db, tenant_id) as session:
        link = await session.scalar(
            select(PoFactoryLink).where(
                PoFactoryLink.token_hash == token_hash,
                PoFactoryLink.revoked_at.is_(None),
                PoFactoryLink.expires_at > func.now(),
            )
        )
        if link is None:
            raise FactoryLinkInvalid(INVALID_LINK)
        access = FactoryAccess(
            tenant_id=tenant_id,
            link_id=link.id,
            payment_id=link.payment_id,
            expires_at=link.expires_at,
        )
        await session.execute(
            update(PoFactoryLink)
            .where(
                PoFactoryLink.id == link.id,
                or_(
                    PoFactoryLink.last_used_at.is_(None),
                    PoFactoryLink.last_used_at < func.now() - LAST_USED_EVERY,
                ),
            )
            .values(last_used_at=func.now())
            .execution_options(synchronize_session=False)
        )
    return access


async def accept_order(db: Db, access: FactoryAccess) -> FactoryAcceptedView:
    """The factory confirms its order: once (a repeat answers the first time), audited once.
    It uploads documents only after that."""
    accepted_now = False
    async with tenant_session(db, access.tenant_id) as session:
        link = await active_link(session, access, lock=True)
        accepted_at = link.accepted_at
        if accepted_at is None:
            accepted_at = datetime.now(UTC)
            link.accepted_at = accepted_at
            payment = await session.get(PoPayment, link.payment_id)
            if payment is None:
                raise FactoryLinkInvalid(INVALID_LINK)
            po = await _order(session, payment)
            session.add(
                rfq_event(
                    access.tenant_id,
                    None,
                    FACTORY_ORDER_ACCEPTED,
                    po.rfq_id,
                    payment_id=payment.id,
                    link_id=link.id,
                    purchase_order_id=po.id,
                    purchase_order_ref=po.ref,
                )
            )
            await session.flush()
            accepted_now = True
    if accepted_now:
        logger.info(
            "factory accepted order tenant=%s payment=%s link=%s",
            access.tenant_id,
            access.payment_id,
            access.link_id,
        )
    return FactoryAcceptedView(accepted_at=accepted_at)
