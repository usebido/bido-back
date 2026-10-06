"""How the RFQ flow gets an `EmailSender` for a buyer's own mailbox.

RFQ emails are sent from the requesting buyer's connected mailbox (Gmail or
Microsoft 365). The connection, tokens and provider transport live in
`bido_back.mailbox`; this module is the seam the RFQ code depends on:

    MailboxSenderFactory(session, tenant_id, user_id) -> EmailSender

raising `rfqs.errors.MailboxNotConnected` when the user has no usable
connection. The default implementation imports `bido_back.mailbox` on first
use (it is built separately) and translates its own `MailboxNotConnected`
into ours, so the rest of the RFQ code and its tests never import it.

The session passed in is the caller's tenant-scoped session; the sender may
use it (token refresh, storing the provider thread id), so the caller keeps
the session open until the send has finished.
"""

import importlib
import inspect
from collections.abc import Awaitable, Callable
from typing import Final, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.email.types import EmailSender
from bido_back.rfqs.errors import MailboxNotConnected

MAILBOX_MODULE: Final = "bido_back.mailbox"

MailboxSenderFactory = Callable[[AsyncSession, UUID, UUID], Awaitable[EmailSender]]


async def default_mailbox_sender(
    session: AsyncSession, tenant_id: UUID, user_id: UUID
) -> EmailSender:
    module = importlib.import_module(MAILBOX_MODULE)
    factory: object = getattr(module, "get_mailbox_sender", None)
    not_connected: object = getattr(module, "MailboxNotConnected", None)
    if not callable(factory):
        raise RuntimeError(f"{MAILBOX_MODULE}.get_mailbox_sender is missing")
    expected: type[Exception] = (
        not_connected
        if isinstance(not_connected, type) and issubclass(not_connected, Exception)
        else MailboxNotConnected
    )
    try:
        result: object = cast(Callable[[AsyncSession, UUID, UUID], object], factory)(
            session, tenant_id, user_id
        )
        if inspect.isawaitable(result):
            result = await cast(Awaitable[object], result)
    except expected:
        raise MailboxNotConnected(f"user {user_id} has no connected mailbox") from None
    if not callable(getattr(result, "send", None)):
        raise RuntimeError(f"{MAILBOX_MODULE}.get_mailbox_sender did not return an EmailSender")
    return cast(EmailSender, result)
