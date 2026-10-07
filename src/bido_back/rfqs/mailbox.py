"""How the RFQ flow gets an `EmailSender` for a buyer's own mailbox.

RFQ emails are sent from the requesting buyer's connected mailbox (Gmail or
Microsoft 365). The connection, tokens and provider transport live in
`bido_back.mailbox`; this module is the seam the RFQ code depends on:

    MailboxSenderFactory(session, tenant_id, user_id) -> EmailSender

raising `rfqs.errors.MailboxNotConnected` when the user has no usable
connection, and `rfqs.errors.MailboxNotConfigured` when this deployment cannot
use mailboxes at all (no token encryption key). `bido_back.mailbox` is
imported on first use, so the rest of the RFQ code and its tests never import
it.

`mailbox_senders_for(settings)` picks the factory from `EMAIL_TRANSPORT`:
"mailbox" is the product behavior; "smtp" / "ses" send every RFQ email through
that one transport instead (Mailpit locally), refused outside local and test.
Outside prd, either is wrapped by `bido_back.simulator.sandbox_aware`: a
sandbox tenant's email goes to the supplier simulator, never out.

The session passed in is the caller's tenant-scoped session; the sender may
use it (token refresh, storing the provider thread id), so the caller keeps
the session open until the send has finished.
"""

from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.email.types import EmailSender
from bido_back.rfqs.errors import MailboxNotConfigured, MailboxNotConnected
from bido_back.settings import Settings

MailboxSenderFactory = Callable[[AsyncSession, UUID, UUID], Awaitable[EmailSender]]

_NON_MAILBOX_ENVIRONMENTS = frozenset({"local", "test"})


async def default_mailbox_sender(
    session: AsyncSession, tenant_id: UUID, user_id: UUID
) -> EmailSender:
    from bido_back import mailbox

    try:
        return await mailbox.get_mailbox_sender(session, tenant_id, user_id)
    except mailbox.MailboxNotConnected:
        raise MailboxNotConnected(f"user {user_id} has no connected mailbox") from None
    except mailbox.MailboxNotConfigured:
        raise MailboxNotConfigured("mailbox sending is not configured on this server") from None


def mailbox_senders_for(settings: Settings) -> MailboxSenderFactory:
    """The sender factory for `settings.email_transport` (see the module docstring)."""
    factory = transport_senders(settings)
    if settings.environment == "prd":
        return factory
    from bido_back.simulator.sender import sandbox_aware, sqs_reply_schedulers

    return sandbox_aware(factory, sqs_reply_schedulers(settings))


def transport_senders(settings: Settings) -> MailboxSenderFactory:
    """The factory for `EMAIL_TRANSPORT` alone, without the sandbox wrapper."""
    if settings.email_transport == "mailbox":
        return default_mailbox_sender
    if settings.environment not in _NON_MAILBOX_ENVIRONMENTS:
        raise ValueError(
            f"EMAIL_TRANSPORT={settings.email_transport} is for local and test runs only; "
            "RFQ emails go out from the buyer's mailbox"
        )
    from bido_back.email import build_email_sender

    transport = build_email_sender(settings)

    async def shared_transport(
        session: AsyncSession, tenant_id: UUID, user_id: UUID
    ) -> EmailSender:
        return transport

    return shared_transport
