"""The last check before an email leaves the buyer's mailbox.

`guarded(factory)` wraps a mailbox sender factory: every `send` first checks
the recipients against the tenant's rule (`outbound.domains`) in the caller's
session, and raises `RecipientDomainsNotConfirmed` instead of sending. The
approval guard and the mandate checks stop such emails earlier; this is the
net under them, so no send path can skip the rule.
"""

from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.email.types import EmailSender, OutboundEmail, SentEmail
from bido_back.outbound.domains import RecipientDomainsNotConfirmed, unconfirmed_domains

# Same shape as `bido_back.rfqs.mailbox.MailboxSenderFactory` (not imported:
# that module wraps its factories with `guarded`).
SenderFactory = Callable[[AsyncSession, UUID, UUID], Awaitable[EmailSender]]


class GuardedSender:
    def __init__(self, inner: EmailSender, session: AsyncSession, tenant_id: UUID) -> None:
        self.inner = inner
        self._session = session
        self._tenant_id = tenant_id
        # Callers read the connected mailbox's address when the sender has one.
        self.address: object = getattr(inner, "address", None)

    async def send(self, email: OutboundEmail) -> SentEmail:
        blocked = await unconfirmed_domains(self._session, self._tenant_id, [*email.to, *email.cc])
        if blocked:
            raise RecipientDomainsNotConfirmed(blocked)
        return await self.inner.send(email)


def guarded(factory: SenderFactory) -> SenderFactory:
    async def make(session: AsyncSession, tenant_id: UUID, user_id: UUID) -> EmailSender:
        return GuardedSender(await factory(session, tenant_id, user_id), session, tenant_id)

    return make
