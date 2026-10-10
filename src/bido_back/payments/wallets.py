"""The company's invisible wallet: one Privy server wallet per tenant, owned by Bido.

The wallet is created on first use with `external_id = "tenant-<tenant uuid>"`
(unique per Privy app, so a company has one wallet whatever the races), owned
by the key quorum `PRIVY_WALLET_OWNER_ID` (which holds Bido's P-256
authorization key) and bound to `PRIVY_WALLET_POLICY_ID` when it is set. No
Privy user, login or seed phrase exists; the buyer never sees the wallet, and
it needs no SOL: the relayer pays every fee.

Creating a wallet needs no authorization signature, so anyone holding the app
secret could create one under our external id. A wallet is therefore used only
when Privy says it is a Solana wallet owned by our quorum; anything else is
refused, never minted into.
"""

import logging
from typing import Final
from uuid import UUID, uuid4

import httpx

from bido_back.payments.errors import PaymentsNotConfigured, ProviderError
from bido_back.payments.ports import CustodyWallet, WalletProvider
from bido_back.payments.providers.http import BAD_RESPONSE, loggable_id, path_segment
from bido_back.payments.providers.privy import PROVIDER as PRIVY
from bido_back.payments.providers.privy import SOLANA, PrivyClient, Wallet
from bido_back.settings import Settings

logger = logging.getLogger(__name__)

WALLET_CHAIN_MISMATCH: Final = "wallet_chain_mismatch"
WALLET_OWNER_MISMATCH: Final = "wallet_owner_mismatch"
WALLET_EXTERNAL_ID_MISMATCH: Final = "wallet_external_id_mismatch"

_BASE58: Final = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def tenant_external_id(tenant_id: UUID) -> str:
    return f"tenant-{tenant_id}"


def is_solana_address(value: str) -> bool:
    """`value` is base58 for exactly 32 bytes (an ed25519 public key or a PDA)."""
    if not 32 <= len(value) <= 44 or any(char not in _BASE58 for char in value):
        return False
    number = 0
    for char in value:
        number = number * 58 + _BASE58.index(char)
    leading_zero_bytes = len(value) - len(value.lstrip("1"))
    return leading_zero_bytes + (number.bit_length() + 7) // 8 == 32


class PrivyWalletProvider:
    """`ports.WalletProvider` on Privy server wallets."""

    def __init__(self, client: PrivyClient, *, owner_id: str, policy_id: str | None = None) -> None:
        self._client = client
        self._owner_id = owner_id
        self._policy_ids = (policy_id,) if policy_id else ()

    async def ensure_wallet(self, *, tenant_id: UUID) -> CustodyWallet:
        external_id = tenant_external_id(tenant_id)
        wallet = await self._client.get_wallet_by_external_id(external_id)
        if wallet is None:
            wallet = await self._create(external_id)
            logger.info("privy wallet for tenant=%s wallet=%s", tenant_id, loggable_id(wallet.id))
        return self._checked(wallet, external_id, tenant_id)

    async def sign_transaction(
        self, *, wallet_ref: str, transaction: bytes, idempotency_key: str
    ) -> bytes:
        """The wallet's signature added to `transaction` (Privy signs, never broadcasts).

        Privy caches a failure under `idempotency_key`: retry with a new key.
        """
        return await self._client.sign_transaction(
            wallet_ref, transaction, idempotency_key=idempotency_key
        )

    async def _create(self, external_id: str) -> Wallet:
        try:
            # A new key per attempt: Privy replays a cached error for a reused key.
            return await self._client.create_wallet(
                external_id=external_id,
                owner_id=self._owner_id,
                policy_ids=self._policy_ids,
                idempotency_key=str(uuid4()),
            )
        except ProviderError:
            # Another first payment of the same company may have created it (the
            # external id is unique), or a timeout hid our own success: look again.
            try:
                existing = await self._client.get_wallet_by_external_id(external_id)
            except ProviderError:
                existing = None
            if existing is None:
                raise
            return existing

    def _checked(self, wallet: Wallet, external_id: str, tenant_id: UUID) -> CustodyWallet:
        op = "ensure_wallet"
        problem: str | None = None
        if wallet.chain_type != SOLANA:
            problem = WALLET_CHAIN_MISMATCH
        elif wallet.owner_id != self._owner_id:
            problem = WALLET_OWNER_MISMATCH
        elif wallet.external_id is not None and wallet.external_id != external_id:
            problem = WALLET_EXTERNAL_ID_MISMATCH
        elif not is_solana_address(wallet.address):
            problem = BAD_RESPONSE
        if problem is not None:
            logger.warning(
                "privy wallet refused tenant=%s wallet=%s code=%s",
                tenant_id,
                loggable_id(wallet.id),
                problem,
            )
            raise ProviderError(PRIVY, op, problem)
        return CustodyWallet(
            ref=path_segment(wallet.id, provider=PRIVY, operation=op), address=wallet.address
        )


def build_wallet_provider(
    settings: Settings, *, http: httpx.AsyncClient | None = None
) -> WalletProvider:
    """The wallet provider from settings (`PRIVY_*`)."""
    app_id = _required(settings.privy_app_id, "PRIVY_APP_ID")
    app_secret = _required(settings.privy_app_secret, "PRIVY_APP_SECRET")
    authorization_key = _required(settings.privy_authorization_key, "PRIVY_AUTHORIZATION_KEY")
    owner_id = _required(settings.privy_wallet_owner_id, "PRIVY_WALLET_OWNER_ID")
    try:
        client = PrivyClient(
            app_id, app_secret, authorization_key, base_url=settings.privy_api_url, http=http
        )
    except ValueError:
        raise PaymentsNotConfigured(
            "PRIVY_AUTHORIZATION_KEY is not a base64 PKCS#8 P-256 private key"
        ) from None
    return PrivyWalletProvider(client, owner_id=owner_id, policy_id=settings.privy_wallet_policy_id)


def _required(value: str | None, name: str) -> str:
    if not value:
        raise PaymentsNotConfigured(f"payments are not set up: missing {name}")
    return value
