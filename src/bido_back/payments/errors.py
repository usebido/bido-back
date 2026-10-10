"""Why a payment operation was refused or failed.

Messages carry ids, codes and reasons only: never card or bank data, amounts,
names, provider response bodies or keys. They end up in logs, Sentry and HTTP
responses.
"""


class PaymentError(Exception):
    """Base class for every payment refusal or failure."""


class PaymentsNotConfigured(PaymentError):
    """This server has no payment providers set up (HTTP 503)."""


class PaymentNotFound(PaymentError):
    """No such row in the caller's tenant (another tenant's id looks the same)."""


class PaymentInvalidState(PaymentError):
    """The operation does not fit the current state (HTTP 409)."""


class PaymentForbidden(PaymentError):
    """The caller's role does not allow this (re-read from `memberships`)."""


class PaymentRefused(PaymentError):
    """The request cannot be honored as given (HTTP 422): bad milestones, unusable card, ..."""


class ProviderError(PaymentError):
    """A provider call failed. Carries the provider, the operation and a code, nothing else.

    `retryable` says whether repeating the same call (same idempotency key) is safe
    and may succeed: timeouts, 429 and 5xx on idempotent operations.
    """

    def __init__(
        self,
        provider: str,
        operation: str,
        code: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(f"{provider} {operation} failed: {code}")
        self.provider = provider
        self.operation = operation
        self.code = code
        self.status = status
        self.retryable = retryable


class CardCredentialLost(PaymentError):
    """The card's single-use token was minted but no charge exists: nothing was charged.

    The allowance's capacity is spent; a new payment request starts over.
    """


class CardNotVerified(PaymentError):
    """The card network's rail is not active on the allowance: the cardholder has not
    approved it with the passkey (or it expired). Nothing was minted or charged."""
