"""Hot keys: a `HotKey` never shows its secret, and a bad setting never echoes it."""

import copy
import json
import logging
import pickle
import traceback

import pytest
from solders.signature import Signature

from bido_back.payments.chain.keys import HotKey, load_hot_key
from bido_back.payments.errors import PaymentsNotConfigured
from tests.payments.chain.vectors import harness_keypair

RELAYER = harness_keypair("relayer")
OPERATOR = harness_keypair("operator")
MESSAGE = b"bido:hot-key-test-message"


def test_a_hot_key_shows_its_role_never_its_secret() -> None:
    hot = HotKey(RELAYER, "relayer")
    secret = str(RELAYER)  # `str(Keypair)` is the base58 secret

    shown = [str(hot), repr(hot), f"{hot}", f"{hot!r}", format(hot), repr([hot])]

    assert shown[:5] == ["HotKey(relayer)"] * 5
    assert all(secret not in text for text in shown)


def test_a_hot_key_is_safe_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    hot = HotKey(RELAYER, "relayer")
    logger = logging.getLogger("tests.payments.chain.keys")

    with caplog.at_level(logging.DEBUG, logger="tests.payments.chain.keys"):
        logger.info("relayer %s %r", hot, hot)
        logger.info("relayer.loaded", extra={"hot_key": hot})

    assert caplog.records[0].getMessage() == "relayer HotKey(relayer) HotKey(relayer)"
    assert str(RELAYER) not in caplog.text


def test_a_hot_key_cannot_be_serialized_pickled_or_copied() -> None:
    hot = HotKey(RELAYER, "relayer")

    with pytest.raises(TypeError):
        json.dumps({"relayer": hot})
    with pytest.raises(TypeError):
        pickle.dumps(hot)
    with pytest.raises(TypeError):
        copy.copy(hot)
    with pytest.raises(TypeError):
        copy.deepcopy(hot)
    with pytest.raises(TypeError):
        vars(hot)  # no instance dict to dump


def test_a_hot_key_signs_with_its_keypair() -> None:
    hot = HotKey(RELAYER, "relayer")

    signature = hot.sign_message(MESSAGE)

    assert (hot.pubkey(), hot.role) == (RELAYER.pubkey(), "relayer")
    assert signature == RELAYER.sign_message(MESSAGE)
    assert signature.verify(RELAYER.pubkey(), MESSAGE)


def test_a_key_loads_from_its_base58_secret() -> None:
    hot = load_hot_key(str(RELAYER), setting="SOLANA_RELAYER_KEY", role="relayer")
    assert hot.pubkey() == RELAYER.pubkey()
    padded = load_hot_key(f"  {RELAYER}\n", setting="SOLANA_RELAYER_KEY", role="relayer")
    assert padded.pubkey() == RELAYER.pubkey()


@pytest.mark.parametrize("missing", [None, ""])
def test_a_missing_key_names_its_setting(missing: str | None) -> None:
    with pytest.raises(PaymentsNotConfigured, match="SOLANA_OPERATOR_KEY is not set"):
        load_hot_key(missing, setting="SOLANA_OPERATOR_KEY", role="operator")


def mismatched_halves() -> str:
    """64 bytes in base58 whose public half belongs to another key."""
    return str(Signature.from_bytes(bytes(RELAYER)[:32] + bytes(OPERATOR.pubkey())))


@pytest.mark.parametrize(
    "malformed",
    [
        "0OIl-not-base58",
        str(RELAYER.pubkey()),  # 32 bytes: a public key, not a secret
        mismatched_halves(),
        str(RELAYER) + "1",
        "[1, 2, 3]",
    ],
    ids=["not-base58", "32-bytes", "mismatched-halves", "65-bytes", "json"],
)
def test_a_malformed_key_is_never_echoed_or_chained(malformed: str) -> None:
    with pytest.raises(PaymentsNotConfigured) as caught:
        load_hot_key(malformed, setting="SOLANA_RELAYER_KEY", role="relayer")

    error = caught.value
    assert str(error) == "SOLANA_RELAYER_KEY is not a base58 64-byte secret key"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert malformed not in "".join(traceback.format_exception(error))
