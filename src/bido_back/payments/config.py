"""Which payment providers this server has, built from settings.

Payments are off unless every required setting is present: the API answers
503 and the worker registers nothing that could move money. The builders are
imported lazily, so a server without payments never imports the providers.
"""

from dataclasses import dataclass

import httpx

from bido_back.payments.errors import PaymentsNotConfigured
from bido_back.payments.ports import BankDetailsCipher, CardGateway, PaymentChain, WalletProvider
from bido_back.settings import Settings


@dataclass(frozen=True, slots=True)
class PaymentProviders:
    card: CardGateway
    bank_details: BankDetailsCipher
    wallets: WalletProvider
    chain: PaymentChain


def missing_payment_settings(settings: Settings) -> list[str]:
    """The environment variables payments still need (names only, never values)."""
    required: dict[str, str | None] = {
        "BASIS_THEORY_API_KEY": settings.basis_theory_api_key,
        "PRIVY_APP_ID": settings.privy_app_id,
        "PRIVY_APP_SECRET": settings.privy_app_secret,
        "PRIVY_AUTHORIZATION_KEY": settings.privy_authorization_key,
        "PRIVY_WALLET_OWNER_ID": settings.privy_wallet_owner_id,
        "SOLANA_RPC_URL": settings.solana_rpc_url,
        "SOLANA_STABLECOIN_MINT": settings.solana_stablecoin_mint,
        "SOLANA_MINT_AUTHORITY_KEY": settings.solana_mint_authority_key,
        "SOLANA_OPERATOR_KEY": settings.solana_operator_key,
        "SOLANA_RELAYER_KEY": settings.solana_relayer_key,
        "SOLANA_PAYOUT_OWNER": settings.solana_payout_owner,
        "SOLANA_GATE_ATTESTER": settings.solana_gate_attester,
    }
    if not (settings.payout_details_kms_key_id or settings.payout_details_encryption_key):
        required["PAYOUT_DETAILS_KMS_KEY_ID or PAYOUT_DETAILS_ENCRYPTION_KEY"] = None
    if settings.payments_card_charge == "stripe":
        required["STRIPE_SECRET_KEY"] = settings.stripe_secret_key
        required["STRIPE_NETWORK_BUSINESS_PROFILE"] = settings.stripe_network_business_profile
    return [name for name, value in required.items() if not value]


def payments_enabled(settings: Settings) -> bool:
    return not missing_payment_settings(settings)


def missing_release_settings(settings: Settings) -> list[str]:
    """What milestone releases still need beyond payments (names only, never values): the
    gate attester's secret key, which signs each approved milestone's attestation."""
    missing = missing_payment_settings(settings)
    if not settings.solana_gate_attester_key:
        missing.append("SOLANA_GATE_ATTESTER_KEY")
    return missing


def releases_enabled(settings: Settings) -> bool:
    """Milestones can be submitted and released: without it nothing is proposed that the
    worker could not carry out."""
    return not missing_release_settings(settings)


def build_payment_providers(
    settings: Settings, *, http: httpx.AsyncClient | None = None
) -> PaymentProviders:
    """Every provider, from settings. Raises `PaymentsNotConfigured` naming what is missing."""
    missing = missing_payment_settings(settings)
    if missing:
        raise PaymentsNotConfigured(f"payments are not set up: missing {', '.join(missing)}")
    from bido_back.payments.bank_details import build_bank_details_cipher
    from bido_back.payments.card import build_card_gateway
    from bido_back.payments.chain import build_payment_chain
    from bido_back.payments.wallets import build_wallet_provider

    return PaymentProviders(
        card=build_card_gateway(settings, http=http),
        bank_details=build_bank_details_cipher(settings),
        wallets=build_wallet_provider(settings, http=http),
        chain=build_payment_chain(settings, http=http),
    )


def explorer_tx_url(cluster: str, signature: str) -> str:
    return f"https://explorer.solana.com/tx/{signature}?cluster={cluster}"
