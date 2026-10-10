"""The relayer's, the operator's and the mint authority's hot keys.

`SOLANA_RELAYER_KEY`, `SOLANA_OPERATOR_KEY` and `SOLANA_MINT_AUTHORITY_KEY` are base58
64-byte secret keys that reach the process only through `Settings` (Secrets Manager
in AWS). A raw `Keypair` never leaves this module, because `str(Keypair)` is the
base58 secret and pickling or `to_json()` reveal it too: callers get a `HotKey`,
which tells its public key and signs, nothing else. A missing or malformed key is
`PaymentsNotConfigured` naming the setting, never its value, and the parse error is
not chained to it.
"""

from typing import NoReturn, SupportsIndex, final

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature

from bido_back.payments.errors import PaymentsNotConfigured


@final
class HotKey:
    """A signing key held in memory: `role`, `pubkey()` and `sign_message()` only.

    `str` and `repr` (and so logging and f-strings) show the role, never the key;
    `json.dumps` refuses it like any object, and pickling or copying raises `TypeError`.
    """

    __slots__ = ("_keypair", "_role")

    def __init__(self, keypair: Keypair, role: str) -> None:
        self._keypair = keypair
        self._role = role

    @property
    def role(self) -> str:
        return self._role

    def pubkey(self) -> Pubkey:
        return self._keypair.pubkey()

    def sign_message(self, message: bytes) -> Signature:
        """The ed25519 signature of `message` (a v0 message: `to_bytes_versioned`)."""
        return self._keypair.sign_message(message)

    def __repr__(self) -> str:
        return f"HotKey({self._role})"

    def __str__(self) -> str:
        return self.__repr__()

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        raise TypeError("a HotKey cannot be pickled or copied")


def load_hot_key(secret: str | None, *, setting: str, role: str) -> HotKey:
    """The key in setting `setting` (a base58 64-byte secret key) as a `HotKey`."""
    if not secret:
        raise PaymentsNotConfigured(f"{setting} is not set")
    keypair: Keypair | None
    try:
        keypair = Keypair.from_base58_string(secret.strip())
    except ValueError:
        keypair = None
    if keypair is None:
        # Raised outside the handler: the parse error is not chained, so no traceback
        # reaches the value.
        raise PaymentsNotConfigured(f"{setting} is not a base58 64-byte secret key")
    return HotKey(keypair, role)
