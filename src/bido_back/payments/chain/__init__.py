"""Solana for payments: Bido's stablecoin mint and the `bido_escrow` program (`solana/`).

`build_payment_chain(settings)` returns the `PaymentChain` the payment pipeline uses
(`solana.SolanaPaymentChain`). solders types never leave this package.

- `hashes`: the escrow id and the payout reference (standard library only; the service
  imports it before anything reaches Solana).
- `idl`, `borsh`, `pdas`, `escrow`, `spl`, `sas`: the programs' codec (the escrow, SPL
  Token and Memo, the Solana Attestation Service), pinned by the golden vectors of
  `solana/tests/fixtures/vectors.json`.
- `rpc`: Solana JSON-RPC over httpx. `keys`: the relayer's, the operator's, the mint
  authority's and the gate attester's hot keys. `transactions`: v0 messages and their
  signatures.

Importing this package does not import solders: `build_payment_chain` loads the
adapter on first use, so a server without payments never does.
"""

import httpx

from bido_back.payments.ports import PaymentChain
from bido_back.settings import Settings


def build_payment_chain(
    settings: Settings, *, http: httpx.AsyncClient | None = None
) -> PaymentChain:
    """The chain adapter; `PaymentsNotConfigured` names any missing or malformed setting.

    `http` is injected by tests (an `httpx.MockTransport`); without it the adapter opens
    its own client with explicit timeouts.
    """
    from bido_back.payments.chain.solana import SolanaPaymentChain

    return SolanaPaymentChain.from_settings(settings, http=http)
