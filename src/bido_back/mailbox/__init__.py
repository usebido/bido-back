"""The buyer's own mailbox (Gmail / Microsoft 365), used only in RFQ threads Bido started.

See `bido_back.mailbox.service` for the scope and the flows.
"""

from bido_back.mailbox.base import MailboxNotConfigured, MailboxNotConnected
from bido_back.mailbox.service import (
    ALL_TENANTS,
    MailboxSyncDeps,
    MailboxSyncHandler,
    get_mailbox_sender,
    handle_mailbox_sync,
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
    "sync_rfq_threads",
]
