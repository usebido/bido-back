"""Which recipient domains a tenant's outbound email may go to.

The rule (tenants with `outbound_requires_confirmed_domains`, the default):
an email may go to a recipient only if its domain is a reserved test domain
(`bido_back.email.recipients.is_test_domain`) or a domain a person of the
tenant confirmed (`outbound_domain_confirmations`). The decision uses the
domain alone, never DNS, so a slow or failing lookup cannot let anything out.
DNS (`check_recipients`) only tells people what they are confirming: a domain
that receives mail ("real company"), one that does not, or unknown.

Confirmation happens with an approval (`confirmed_domains` on the decision,
checked by the approval guard) or through `/v1/outbound/domains`. The mailbox
sender re-checks every email right before it is sent (`GuardedSender`).
"""

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, cast
from uuid import UUID

from pydantic import BaseModel, JsonValue
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.db.models import OutboundDomainConfirmation, Tenant
from bido_back.email.recipients import (
    MxResolver,
    RecipientStatus,
    classify,
    domain_of,
    is_test_domain,
)

_MAX_DOMAIN: Final = 253


class RecipientDomainsNotConfirmed(Exception):
    """An email would go to domains nobody confirmed. Carries the domains only."""

    def __init__(self, domains: Sequence[str]) -> None:
        self.domains = sorted(set(domains))
        super().__init__(
            "recipient domains need a person's confirmation before Bido sends: "
            + ", ".join(self.domains)
        )


class RecipientCheck(BaseModel):
    """One recipient, as shown to the person who approves the email."""

    address: str
    domain: str | None
    status: RecipientStatus
    confirmed: bool
    # True when the email would go out to this address as things stand.
    will_send: bool


def normalize_domain(value: str) -> str | None:
    d = value.strip().lower().rstrip(".")
    if d.startswith("@"):
        d = d[1:]
    if "@" in d or not d or len(d) > _MAX_DOMAIN or "." not in d or " " in d:
        return None
    return d


def recipients_in_args(args: Mapping[str, JsonValue]) -> list[str]:
    """The addresses an act would write to: `to` and `cc`, or every mandated
    supplier's `to`. Empty for acts that send no email."""
    found: list[str] = []

    def take(value: object) -> None:
        if isinstance(value, list):
            for item in cast(list[object], value):
                if isinstance(item, str):
                    found.append(item)

    take(args.get("to"))
    take(args.get("cc"))
    suppliers = args.get("suppliers")
    if isinstance(suppliers, list):
        for supplier in cast(list[object], suppliers):
            if isinstance(supplier, dict):
                take(cast(dict[str, Any], supplier).get("to"))
    return list(dict.fromkeys(found))


async def requires_confirmation(session: AsyncSession, tenant_id: UUID) -> bool:
    value = await session.scalar(
        select(Tenant.outbound_requires_confirmed_domains).where(Tenant.id == tenant_id)
    )
    # A tenant that cannot be read is guarded.
    return True if value is None else value


async def confirmed_domains(session: AsyncSession, tenant_id: UUID) -> set[str]:
    rows = await session.scalars(
        select(OutboundDomainConfirmation.domain).where(
            OutboundDomainConfirmation.tenant_id == tenant_id
        )
    )
    return set(rows)


async def confirm_domains(
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    domains: Iterable[str],
    approval_id: UUID | None = None,
) -> list[str]:
    """Record confirmations (idempotent). Returns the normalized domains recorded."""
    clean = sorted({d for d in (normalize_domain(x) for x in domains) if d is not None})
    for domain in clean:
        await session.execute(
            insert(OutboundDomainConfirmation)
            .values(
                tenant_id=tenant_id,
                domain=domain,
                confirmed_by=user_id,
                approval_id=approval_id,
            )
            .on_conflict_do_nothing(constraint="outbound_domain_confirmations_tenant_domain_key")
        )
    return clean


def _blocked(addresses: Iterable[str], required: bool, confirmed: set[str]) -> list[str]:
    blocked: set[str] = set()
    for address in addresses:
        domain = domain_of(address)
        if domain is None:
            blocked.add(address.strip().lower() or "<empty>")
        elif required and not is_test_domain(domain) and domain not in confirmed:
            blocked.add(domain)
    return sorted(blocked)


async def unconfirmed_domains(
    session: AsyncSession, tenant_id: UUID, addresses: Iterable[str]
) -> list[str]:
    """Domains (or malformed addresses) that block sending to `addresses`. No DNS."""
    required = await requires_confirmation(session, tenant_id)
    confirmed = await confirmed_domains(session, tenant_id) if required else set[str]()
    return _blocked(addresses, required, confirmed)


async def check_recipients(
    session: AsyncSession,
    tenant_id: UUID,
    addresses: Sequence[str],
    resolver: MxResolver,
) -> list[RecipientCheck]:
    """Per-address view for people: DNS status, confirmed, and whether it would go out."""
    if not addresses:
        return []
    required = await requires_confirmation(session, tenant_id)
    confirmed = await confirmed_domains(session, tenant_id)
    statuses = await asyncio.gather(*(classify(a, resolver) for a in addresses))
    checks: list[RecipientCheck] = []
    for address, status in zip(addresses, statuses, strict=True):
        domain = domain_of(address)
        is_confirmed = domain is not None and domain in confirmed
        will_send = status != "invalid" and (not required or status == "test" or is_confirmed)
        checks.append(
            RecipientCheck(
                address=address,
                domain=domain,
                status=status,
                confirmed=is_confirmed,
                will_send=will_send,
            )
        )
    return checks
