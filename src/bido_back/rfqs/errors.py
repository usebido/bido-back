"""Why an RFQ operation was refused. Messages carry ids and reasons, never content."""


class RfqError(Exception):
    """Base class for refusals of the RFQ services."""


class NotFound(RfqError):
    """No such row in the caller's tenant (another tenant's id looks the same)."""


class InvalidState(RfqError):
    """The operation does not fit the current status of the RFQ or invite."""


class ReplyByRequired(InvalidState):
    """An RFQ email needs a reply-by date (`rfqs.response_due`) of today or later.

    `draft` says whether the RFQ is still a draft, whose terms the copilot can set;
    an open RFQ's reply-by date is set through its follow-up settings."""

    def __init__(self, message: str, *, draft: bool) -> None:
        super().__init__(message)
        self.draft = draft


class Duplicate(RfqError):
    """The row already exists (e.g. the supplier is already invited to this RFQ)."""


class AgentFailed(RfqError):
    """An agent run ended without a usable output."""


class MailboxNotConnected(InvalidState):
    """The buyer who would send has no connected mailbox (Gmail / Microsoft 365)."""


class MailboxNotConfigured(InvalidState):
    """This deployment cannot use buyer mailboxes (token encryption is not set up)."""


class CurrencyRequired(RfqError):
    """Nothing tells which currency the RFQ is in: the buyer has to say (no silent default)."""


class UnknownCurrency(RfqError):
    """A currency code Bido cannot convert (not EUR nor an ECB reference currency)."""
