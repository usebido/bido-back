"""The card port on Basis Theory Agentic Payments, charged on Bido's Stripe account.

The buyer's card is tokenized in the browser by Basis Theory Elements; Bido
only ever sees the token id. `enroll` reads the token (fingerprint, brand,
last four, expiry, funding: never the number) and creates an agentic payment
method from it. The card is usable when its `spt` rail (provider `stripe`) is
enabled: Stripe Shared Payment Tokens need no passkey ceremony. The payment
method also carries the issuer's branding (`card.display`, `card.issuer`), which
is kept when well formed (`payments.card_display`) so the buyer sees their card.

Paying one purchase order:

1. `authorize` creates an allowance scoped to Bido (the merchant in settings)
   for exactly the amount, until `expires_at`.
2. `charge` mints the single-use Stripe Shared Payment Token from the
   allowance (Basis Theory key `<key>:credential`), then confirms one
   PaymentIntent with it on Bido's Stripe account (Stripe key `<key>:charge`,
   `metadata[bido_payment_id]`). The token exists in this call only: it is never
   logged, stored or returned.

Exactly once: Basis Theory keeps the mint's outcome under its key, Stripe the
PaymentIntent's under its own. A replayed mint cannot hand the token out again
(`CREDENTIAL_PAYLOAD_UNAVAILABLE`, or `CREDENTIAL_OUTCOME_UNKNOWN`), so the
gateway looks the charge up at Stripe by payment id and returns it, or raises
`CardCredentialLost` when there is none: nothing was charged, and the
allowance's capacity is spent. Since a lost token cannot be minted again, a
transient Stripe failure right after the mint is retried here, under the same
key, before giving up.

Simulated mode (`PAYMENTS_CARD_CHARGE=simulated`, Basis Theory test tenant
only): the allowance and the mint still go to Basis Theory, whose test tenant
mints mock tokens Stripe cannot charge. There is no Stripe client at all; the
charge is `sim_<payment_id>`, marked simulated.

Visa rail (`PAYMENTS_CARD_RAIL=vic`, simulated only for now): the card is
usable when its `agentic-token` rail (provider `vic`) is enabled. Each
payment's allowance starts `pending_verification`: the cardholder approves it
in the browser with the Visa passkey (Basis Theory's `web-agentic` SDK;
Basis Theory's mock of the ceremony on its test tenant), and `verification`
reads where that stands. `charge` then mints a single-use virtual card for the
agent (format `card`, key `<key>:credential`); Basis Theory refuses the mint
while the rail is not active (`CardNotVerified`). Nothing charges that card
yet: the charge is `sim_<payment_id>`, and only the card's last four digits and
expiry leave the gateway, never its number or security code.

Basis Theory is used for cards only: a supplier's bank details are sealed in Bido's
database for the payout provider (`payments.bank_details`).
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import SecretStr

from bido_back.payments.card_display import card_display
from bido_back.payments.errors import (
    CardCredentialLost,
    CardNotVerified,
    PaymentRefused,
    PaymentsNotConfigured,
    ProviderError,
)
from bido_back.payments.ports import SPT_RAIL as SPT_NAMES
from bido_back.payments.ports import VIC_RAIL as VIC_NAMES
from bido_back.payments.ports import (
    CardAuthorization,
    CardCharge,
    CardConsumer,
    CardDisplayData,
    CardGateway,
    CardRail,
    CardStatus,
    CardVerification,
    CardVerificationStatus,
    EnrolledCard,
    IssuedCard,
)
from bido_back.payments.providers.basis_theory import (
    PROVIDER as BASIS_THEORY,
)
from bido_back.payments.providers.basis_theory import (
    Allowance,
    BasisTheoryClient,
    CardDetails,
    CredentialPayloadUnavailable,
    Merchant,
    MintedCard,
    PaymentMethod,
    Rail,
    Token,
    find_rail,
)
from bido_back.payments.providers.http import loggable_id
from bido_back.payments.providers.stripe import PROVIDER as STRIPE
from bido_back.payments.providers.stripe import StripeCharge, StripeClient
from bido_back.settings import BASIS_THEORY_TEST_HOST, Settings

logger = logging.getLogger(__name__)

SPT_RAIL, SPT_PROVIDER = SPT_NAMES
# Visa Intelligent Commerce: Basis Theory's network-token rail with provider `vic`.
VIC_RAIL, VIC_PROVIDER = VIC_NAMES
# What Basis Theory answers when a credential is minted from a rail that is not active.
RAIL_NOT_ACTIVE: Final = "RAIL_NOT_ACTIVE"
_RAILS: Final[dict[CardRail, tuple[str, str]]] = {
    "spt": (SPT_RAIL, SPT_PROVIDER),
    "vic": (VIC_RAIL, VIC_PROVIDER),
}
CHARGE_CURRENCY: Final = "USD"
# Stripe's limits for a USD charge.
MIN_CHARGE: Final = Decimal("0.50")
MAX_CHARGE: Final = Decimal("999999.99")
# Tries of the Stripe charge once the token is minted (same key each time).
STRIPE_CHARGE_ATTEMPTS: Final = 3
STRIPE_RETRY_BACKOFF_S: Final = 1.0
# The Basis Theory test tenant still wants a grantee profile for its mock tokens.
SIMULATED_NETWORK_BUSINESS_PROFILE: Final = "profile_bido_simulated"

MISSING_FINGERPRINT: Final = "missing_fingerprint"
CHARGE_MISMATCH: Final = "charge_mismatch"

_CENT: Final = Decimal("0.01")
_LAST4: Final = re.compile(r"[0-9]{4}")

Sleep = Callable[[float], Awaitable[None]]


def charge_cents(amount: Decimal, currency: str) -> int:
    """`amount` in cents, or `PaymentRefused` for an amount Stripe could not charge."""
    if currency.upper() != CHARGE_CURRENCY:
        raise PaymentRefused("cards are charged in USD")
    if not amount.is_finite() or amount < MIN_CHARGE:
        raise PaymentRefused("a card charge is at least 0.50 USD")
    if amount > MAX_CHARGE:
        raise PaymentRefused("a card charge is at most 999999.99 USD")
    if amount != amount.quantize(_CENT):
        raise PaymentRefused("a card charge is a whole number of cents")
    return int(amount / _CENT)


def _find(rails: tuple[Rail, ...], card_rail: CardRail) -> Rail | None:
    rail, provider = _RAILS[card_rail]
    return find_rail(rails, rail=rail, provider=provider)


def card_status(rails: tuple[Rail, ...], card_rail: CardRail = "spt") -> CardStatus:
    """The card's status on the gateway's rail (`spt`/`stripe` or `agentic-token`/`vic`)."""
    rail = _find(rails, card_rail)
    if rail is None or rail.status == "error":
        return "failed"
    if rail.status == "enabled":
        return "active"
    return "pending"  # `pending`, or a status Basis Theory added since: not usable yet


def allowance_usable(allowance: Allowance, card_rail: CardRail = "spt") -> bool:
    """The allowance is live and its rail can mint (`vic`: once the passkey approved it)."""
    if allowance.status not in (None, "active"):
        return False
    rail = _find(allowance.rails, card_rail)
    return rail is not None and rail.status == "active"


def verification_status(
    allowance: Allowance | None, card_rail: CardRail, *, now: datetime
) -> CardVerificationStatus:
    """Where the cardholder's approval of the allowance stands on the gateway's rail."""
    if allowance is None or allowance.status == "cancelled":
        return "failed"
    expires_at = allowance.expires_at
    if allowance.status == "expired" or (expires_at is not None and expires_at <= now):
        return "expired"
    rail = _find(allowance.rails, card_rail)
    if rail is None or rail.status == "error":
        return "failed"
    if allowance.status in (None, "active") and rail.status == "active":
        return "verified"
    return "pending"  # `pending_verification`, or a status Basis Theory added since


def method_display(method: PaymentMethod) -> CardDisplayData | None:
    """The payment method's branding, well-formed values only (`issuer.name` names the
    issuer when the display does not)."""
    card = method.card or CardDetails()
    display = card.display
    issuer = card.issuer
    return card_display(
        art_url=display.art_url if display else None,
        background_color=display.background_color if display else None,
        foreground_color=display.foreground_color if display else None,
        description=display.description if display else None,
        issuer_name=display.issuer_name if display else None,
        issuer_fallback=issuer.name if issuer else None,
    )


def _enrolled(
    method: PaymentMethod, token: Token, fingerprint: str, card_rail: CardRail
) -> EnrolledCard:
    card = method.card or CardDetails()
    vault = token.card or CardDetails()
    last4 = card.last4 or vault.last4
    month = card.expiration_month or vault.expiration_month
    year = card.expiration_year or vault.expiration_year
    return EnrolledCard(
        payment_method_ref=method.id,
        fingerprint=fingerprint,
        brand=card.brand or vault.brand,
        last4=last4 if last4 is not None and _LAST4.fullmatch(last4) else None,
        exp_month=month if month is not None and 1 <= month <= 12 else None,
        exp_year=year if year is not None and 2000 <= year <= 2999 else None,
        funding=card.funding or vault.funding,
        status=card_status(method.rails, card_rail),
        display=method_display(method),
    )


def _simulated(payment_id: UUID, issued_card: IssuedCard | None = None) -> CardCharge:
    return CardCharge(
        "succeeded", f"sim_{payment_id}", None, simulated=True, issued_card=issued_card
    )


def _issued(card: MintedCard) -> IssuedCard:
    return IssuedCard(
        credential_ref=card.id,
        last4=card.last4 if _LAST4.fullmatch(card.last4) else None,
        exp_month=card.exp_month,
        exp_year=card.exp_year,
    )


def _card_charge(charge: StripeCharge) -> CardCharge:
    return CardCharge(
        charge.outcome, charge.payment_intent_id, charge.decline_code, simulated=False
    )


class BasisTheoryCardGateway:
    """`ports.CardGateway`. Without a Stripe client, charges are simulated; the `vic` rail
    has no Stripe client (nothing charges its virtual card yet)."""

    def __init__(
        self,
        basis_theory: BasisTheoryClient,
        stripe: StripeClient | None,
        *,
        merchant: Merchant,
        network_business_profile: str,
        rail: CardRail = "spt",
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if rail == "vic" and stripe is not None:
            raise PaymentsNotConfigured("the vic rail's virtual card is not charged on Stripe")
        self._basis_theory = basis_theory
        self._stripe = stripe
        self._merchant = merchant
        self._network_business_profile = network_business_profile
        self._rail: CardRail = rail
        self._sleep = sleep

    @property
    def simulated(self) -> bool:
        return self._stripe is None

    @property
    def rail(self) -> CardRail:
        return self._rail

    async def enroll(
        self, *, token_id: str, consumer: CardConsumer, idempotency_key: str
    ) -> EnrolledCard:
        token = await self._basis_theory.get_token(token_id)
        if token is None:
            raise PaymentRefused("the card token was not found or has expired")
        if token.type is not None and token.type != "card":
            raise PaymentRefused("the token is not a card")
        if not token.fingerprint:
            raise ProviderError(BASIS_THEORY, "get_token", MISSING_FINGERPRINT)
        method = await self._basis_theory.create_payment_method(
            token_id=token_id,
            consumer_id=consumer.id,
            email=consumer.email,
            country_code=consumer.country_code,
            idempotency_key=idempotency_key,
        )
        card = _enrolled(method, token, token.fingerprint, self._rail)
        logger.info(
            "card enrolled payment_method=%s status=%s display=%s art=%s",
            loggable_id(card.payment_method_ref),
            card.status,
            card.display is not None,
            card.display is not None and card.display.art_url is not None,
        )
        return card

    async def remove(self, payment_method_ref: str) -> None:
        await self._basis_theory.delete_payment_method(payment_method_ref)

    async def display(self, payment_method_ref: str) -> CardDisplayData | None:
        method = await self._basis_theory.get_payment_method(payment_method_ref)
        return method_display(method) if method is not None else None

    async def authorize(
        self,
        *,
        payment_method_ref: str,
        amount: Decimal,
        currency: str,
        description: str,
        expires_at: datetime,
        idempotency_key: str,
    ) -> CardAuthorization:
        charge_cents(amount, currency)
        allowance = await self._basis_theory.create_allowance(
            payment_method_id=payment_method_ref,
            amount=amount,
            currency=CHARGE_CURRENCY,
            merchant=self._merchant,
            description=description,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
        )
        usable = allowance_usable(allowance, self._rail)
        logger.info("card authorized allowance=%s usable=%s", loggable_id(allowance.id), usable)
        return CardAuthorization(
            ref=allowance.id, usable=usable, expires_at=allowance.expires_at or expires_at
        )

    async def verification(self, authorization_ref: str) -> CardVerification:
        allowance = await self._basis_theory.get_allowance(authorization_ref)
        rail, provider = _RAILS[self._rail]
        status = verification_status(allowance, self._rail, now=datetime.now(UTC))
        logger.info(
            "card verification allowance=%s status=%s", loggable_id(authorization_ref), status
        )
        return CardVerification(status=status, rail=rail, provider=provider)

    async def charge(
        self,
        *,
        authorization_ref: str,
        payment_id: UUID,
        amount: Decimal,
        currency: str,
        description: str,
        idempotency_key: str,
    ) -> CardCharge:
        cents = charge_cents(amount, currency)
        if self._rail == "vic":
            return await self._issue_card(
                authorization_ref, payment_id=payment_id, amount=amount, key=idempotency_key
            )
        try:
            credential = await self._basis_theory.mint_identifier(
                authorization_ref,
                rail=SPT_RAIL,
                provider=SPT_PROVIDER,
                amount=amount,
                currency=CHARGE_CURRENCY,
                network_business_profile=self._network_business_profile,
                idempotency_key=f"{idempotency_key}:credential",
            )
        except CredentialPayloadUnavailable as replayed:
            return await self._charge_made(payment_id, cents, replayed.code)
        if self._stripe is None:
            logger.info("card charge simulated payment=%s", payment_id)
            return _simulated(payment_id)
        charge = await self._confirm(
            self._stripe,
            credential.token,
            cents=cents,
            description=description,
            payment_id=payment_id,
            idempotency_key=f"{idempotency_key}:charge",
        )
        logger.info(
            "card charge %s payment=%s reference=%s decline=%s",
            charge.outcome,
            payment_id,
            loggable_id(charge.payment_intent_id),
            charge.decline_code,
        )
        return _card_charge(charge)

    async def _issue_card(
        self, authorization_ref: str, *, payment_id: UUID, amount: Decimal, key: str
    ) -> CardCharge:
        """Mint the agent's single-use Visa card. Nothing charges it yet: simulated."""
        try:
            card = await self._basis_theory.mint_card(
                authorization_ref,
                rail=VIC_RAIL,
                provider=VIC_PROVIDER,
                amount=amount,
                currency=CHARGE_CURRENCY,
                idempotency_key=f"{key}:credential",
            )
        except CredentialPayloadUnavailable:
            # An earlier attempt minted it; its number is gone, and nothing charges it.
            logger.info("card credential replayed payment=%s", payment_id)
            return _simulated(payment_id)
        except ProviderError as error:
            if error.code == RAIL_NOT_ACTIVE:
                raise CardNotVerified(
                    f"payment {payment_id}: the cardholder has not approved the allowance"
                ) from None
            raise
        logger.info("card issued for the agent payment=%s (not charged)", payment_id)
        return _simulated(payment_id, _issued(card))

    async def _confirm(
        self,
        stripe: StripeClient,
        token: SecretStr,
        *,
        cents: int,
        description: str,
        payment_id: UUID,
        idempotency_key: str,
    ) -> StripeCharge:
        """Charge the minted token; a transient failure is retried under the same key."""
        attempt = 1
        while True:
            try:
                return await stripe.create_payment_intent(
                    shared_payment_token=token,
                    amount_cents=cents,
                    description=description,
                    payment_id=payment_id,
                    idempotency_key=idempotency_key,
                )
            except ProviderError as error:
                if not error.retryable or attempt >= STRIPE_CHARGE_ATTEMPTS:
                    raise
                logger.warning(
                    "card charge retry payment=%s attempt=%s code=%s",
                    payment_id,
                    attempt,
                    error.code,
                )
            await self._sleep(STRIPE_RETRY_BACKOFF_S * attempt)
            attempt += 1

    async def _charge_made(self, payment_id: UUID, cents: int, code: str) -> CardCharge:
        """The charge an earlier attempt made with the token it minted, if any."""
        if self._stripe is None:
            return _simulated(payment_id)
        found = await self._stripe.find_payment_intent(payment_id)
        if found is None:
            logger.warning("card credential lost payment=%s code=%s", payment_id, code)
            raise CardCredentialLost(
                f"payment {payment_id}: the card token was minted but no charge exists"
            )
        if found.amount_cents not in (None, cents) or found.currency not in (None, "usd"):
            raise ProviderError(STRIPE, "find_payment_intent", CHARGE_MISMATCH)
        logger.info(
            "card charge found payment=%s outcome=%s reference=%s",
            payment_id,
            found.outcome,
            loggable_id(found.payment_intent_id),
        )
        return _card_charge(found)


# --- Builders (imported lazily by `payments.config`) -------------------------------------------


def build_card_gateway(settings: Settings, *, http: httpx.AsyncClient | None = None) -> CardGateway:
    """The card gateway from settings: simulated iff `PAYMENTS_CARD_CHARGE=simulated`."""
    basis_theory = _basis_theory(settings, http)
    merchant = Merchant(
        name=settings.payments_merchant_name,
        url=settings.payments_merchant_url,
        country_code=settings.payments_merchant_country,
    )
    rail = settings.payments_card_rail
    if rail == "vic" and settings.payments_card_charge != "simulated":
        raise PaymentsNotConfigured("PAYMENTS_CARD_RAIL=vic needs PAYMENTS_CARD_CHARGE=simulated")
    if settings.payments_card_charge == "simulated":
        # Settings refuse this already; a gateway that never charges must not reach prd.
        test_tenant = urlsplit(settings.basis_theory_api_url).hostname == BASIS_THEORY_TEST_HOST
        if not test_tenant or settings.environment == "prd":
            raise PaymentsNotConfigured(
                "PAYMENTS_CARD_CHARGE=simulated needs the Basis Theory test tenant, outside prd"
            )
        profile = settings.stripe_network_business_profile or SIMULATED_NETWORK_BUSINESS_PROFILE
        return BasisTheoryCardGateway(
            basis_theory, None, merchant=merchant, network_business_profile=profile, rail=rail
        )
    stripe = StripeClient(
        _required(settings.stripe_secret_key, "STRIPE_SECRET_KEY"),
        api_version=settings.stripe_api_version,
        http=http,
    )
    return BasisTheoryCardGateway(
        basis_theory,
        stripe,
        merchant=merchant,
        network_business_profile=_required(
            settings.stripe_network_business_profile, "STRIPE_NETWORK_BUSINESS_PROFILE"
        ),
    )


def _basis_theory(settings: Settings, http: httpx.AsyncClient | None) -> BasisTheoryClient:
    return BasisTheoryClient(
        settings.basis_theory_api_url,
        _required(settings.basis_theory_api_key, "BASIS_THEORY_API_KEY"),
        http=http,
    )


def _required(value: str | None, name: str) -> str:
    if not value:
        raise PaymentsNotConfigured(f"payments are not set up: missing {name}")
    return value
