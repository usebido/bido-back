"""The buyer's own mailbox (Gmail / Microsoft 365): RFQ threads Bido started, plus the
one-off onboarding scan of sent mail the user starts.

See `bido_back.mailbox.service` for the scope and the flows.
"""

from bido_back.mailbox.base import MailboxNotConfigured, MailboxNotConnected
from bido_back.mailbox.service import (
    ALL_TENANTS,
    MailboxSyncDeps,
    MailboxSyncHandler,
    get_mailbox_sender,
    handle_mailbox_sync,
    open_connection,
    sync_rfq_threads,
)

__all__ = [
    "ALL_TENANTS",
    "MailboxNotConfigured",
    "MailboxNotConnected",
    "MailboxSyncDeps",
    "MailboxSyncHandler",
    "get_mailbox_sender",
    "handle_mailbox_sync",
    "open_connection",
    "sync_rfq_threads",
]
