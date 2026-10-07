"""Why an RFQ operation was refused. Messages carry ids and reasons, never content."""


class RfqError(Exception):
    """Base class for refusals of the RFQ services."""


class NotFound(RfqError):
    """No such row in the caller's tenant (another tenant's id looks the same)."""


class InvalidState(RfqError):
    """The operation does not fit the current status of the RFQ or invite."""


class Duplicate(RfqError):
    """The row already exists (e.g. the supplier is already invited to this RFQ)."""


class AgentFailed(RfqError):
    """An agent run ended without a usable output."""


class MailboxNotConnected(InvalidState):
    """The buyer who would send has no connected mailbox (Gmail / Microsoft 365)."""


class MailboxNotConfigured(InvalidState):
    """This deployment cannot use buyer mailboxes (token encryption is not set up)."""
