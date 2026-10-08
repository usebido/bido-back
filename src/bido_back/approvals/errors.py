"""Why an approval operation was refused.

Messages carry ids and reasons only, never the approved arguments: they end
up in logs, Sentry and HTTP responses.
"""


class ApprovalError(Exception):
    """Base class for every refusal of the approval guard."""


class ApprovalNotFound(ApprovalError):
    """No approval with that id in the caller's tenant (another tenant's id looks the same)."""


class ApprovalForbidden(ApprovalError):
    """The caller may not decide this approval (not an approver, or the requester)."""


class ApprovalNotPending(ApprovalError):
    """The approval was already decided; a decision is made exactly once."""


class ApprovalIntegrityError(ApprovalError):
    """The stored arguments no longer match the hash that was proposed."""


class ApprovalNotExecutable(ApprovalError):
    """An execution job for an approval that is not approved, or cannot run as approved."""


class UnknownActTool(ApprovalError):
    """The approved tool is not registered as an act tool in this process."""


class InvalidApprovalJob(ApprovalError):
    """An `approval.execute` envelope that is not one the guard produced."""


class ApprovalRequesterMissing(ApprovalError):
    """A proposal without a requesting user; the approvals schema requires one."""


class ApprovalConflict(ApprovalError):
    """The same tool call was proposed again with a different payload."""


class ApprovalRecipientsUnconfirmed(ApprovalError):
    """Approving would send email to domains nobody confirmed (outbound guard).

    Carries the domains only: the caller confirms them with the decision.
    """

    def __init__(self, domains: list[str]) -> None:
        self.domains = domains
        super().__init__(
            "confirm these recipient domains to approve (confirmed_domains): " + ", ".join(domains)
        )
