"""The card port on Basis Theory Agentic Payments, charged on Bido's Stripe account.

The buyer's card is tokenized in the browser by Basis Theory Elements; Bido
only ever sees the token id. `enroll` reads the token (fingerprint, brand,
last four, expiry, funding: never the number) and creates an agentic payment
method from it. The card is usable when its `spt` rail (provider `stripe`) is
enabled: Stripe Shared Payment Tokens need no passkey ceremony.

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

Basis Theory is used for cards only: a supplier's bank details are sealed in Bido's
database for the payout provider (`payments.bank_details`).
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import SecretStr

from bido_back.payments.errors import (
    CardCredentialLost,
    PaymentRefused,
    PaymentsNotConfigured,
    ProviderError,
)
from bido_back.payments.ports import (
    CardAuthorization,
    CardCharge,
    CardConsumer,
    CardGateway,
    CardStatus,
    EnrolledCard,
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

SPT_RAIL: Final = "spt"
SPT_PROVIDER: Final = "stripe"
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


def card_status(rails: tuple[Rail, ...]) -> CardStatus:
    """The card's status on the rail Bido charges: `spt` with provider `stripe`."""
    rail = find_rail(rails, rail=SPT_RAIL, provider=SPT_PROVIDER)
    if rail is None or rail.status == "error":
        return "failed"
    if rail.status == "enabled":
        return "active"
    return "pending"  # `pending`, or a status Basis Theory added since: not usable yet


def allowance_usable(allowance: Allowance) -> bool:
    """The allowance is live and its `spt` rail can mint (no ceremony on this rail)."""
    if allowance.status not in (None, "active"):
        return False
    rail = find_rail(allowance.rails, rail=SPT_RAIL, provider=SPT_PROVIDER)
    return rail is not None and rail.status == "active"


def _enrolled(method: PaymentMethod, token: Token, fingerprint: str) -> EnrolledCard:
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
        status=card_status(method.rails),
    )


def _simulated(payment_id: UUID) -> CardCharge:
    return CardCharge("succeeded", f"sim_{payment_id}", None, simulated=True)


def _card_charge(charge: StripeCharge) -> CardCharge:
    return CardCharge(
        charge.outcome, charge.payment_intent_id, charge.decline_code, simulated=False
    )


class BasisTheoryCardGateway:
    """`ports.CardGateway`. Without a Stripe client, charges are simulated."""

    def __init__(
        self,
        basis_theory: BasisTheoryClient,
        stripe: StripeClient | None,
        *,
        merchant: Merchant,
        network_business_profile: str,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._basis_theory = basis_theory
        self._stripe = stripe
        self._merchant = merchant
        self._network_business_profile = network_business_profile
        self._sleep = sleep

    @property
    def simulated(self) -> bool:
        return self._stripe is None

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
        card = _enrolled(method, token, token.fingerprint)
        logger.info(
            "card enrolled payment_method=%s status=%s",
            loggable_id(card.payment_method_ref),
            card.status,
        )
        return card

    async def remove(self, payment_method_ref: str) -> None:
        await self._basis_theory.delete_payment_method(payment_method_ref)

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
        usable = allowance_usable(allowance)
        logger.info("card authorized allowance=%s usable=%s", loggable_id(allowance.id), usable)
        return CardAuthorization(
            ref=allowance.id, usable=usable, expires_at=allowance.expires_at or expires_at
        )

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
    if settings.payments_card_charge == "simulated":
        # Settings refuse this already; a gateway that never charges must not reach prd.
        test_tenant = urlsplit(settings.basis_theory_api_url).hostname == BASIS_THEORY_TEST_HOST
        if not test_tenant or settings.environment == "prd":
            raise PaymentsNotConfigured(
                "PAYMENTS_CARD_CHARGE=simulated needs the Basis Theory test tenant, outside prd"
            )
        profile = settings.stripe_network_business_profile or SIMULATED_NETWORK_BUSINESS_PROFILE
        return BasisTheoryCardGateway(
            basis_theory, None, merchant=merchant, network_business_profile=profile
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
