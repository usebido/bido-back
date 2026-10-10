"""The escrow id, the bank fingerprint and the payout reference (`chain/hashes.py`)."""

import re
import subprocess
import sys

import pytest

from bido_back.payments.chain.hashes import (
    CHAIN_PO_ID_RE,
    PAYOUT_REF_MEMO_PREFIX,
    bank_fingerprint,
    is_chain_po_id,
    new_chain_po_id,
    payout_ref,
    payout_ref_memo,
)
from tests.payments.chain.vectors import signing_vectors

CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
FINGERPRINT = "9bbb0c751bd6aabebc30ef882366cd78ad4ee084c03e64a83ad9ed78a6a7398a"


# ---- bank fingerprint and payout reference: the golden vectors ----


def test_bank_fingerprint_equals_every_vector() -> None:
    vectors = signing_vectors()["bank_fingerprint"]
    assert len(vectors) == 3
    for case in vectors:
        fingerprint = bank_fingerprint(case["country"], case["bank_code"], case["account_number"])
        assert fingerprint == case["bank_fingerprint"]


def test_bank_fingerprint_takes_keywords_and_binds_every_field() -> None:
    fingerprint = bank_fingerprint(
        country="CN", bank_code="DEMO-BANK", account_number="DEMO-0000-0042"
    )
    assert fingerprint == FINGERPRINT
    assert bank_fingerprint("CN", "DEMO-BANK", "DEMO-0000-0043") != fingerprint
    assert bank_fingerprint("HK", "DEMO-BANK", "DEMO-0000-0042") != fingerprint
    # The separators keep a field from bleeding into the next.
    assert bank_fingerprint("CN", "DEMO-BANKD", "EMO-0000-0042") != fingerprint


def test_payout_ref_equals_every_vector_and_its_memo() -> None:
    vectors = signing_vectors()["payout_ref"]
    assert len(vectors) == 4
    for case in vectors:
        ref = payout_ref(case["beneficiary_ref"], case["bank_fingerprint"], case["chain_po_id"])
        assert ref == case["payout_ref"]
        assert payout_ref_memo(ref) == case["memo"]
        assert case["memo"] == PAYOUT_REF_MEMO_PREFIX + ref
        assert len(case["memo"].encode()) == 80


def test_payout_ref_takes_keywords() -> None:
    case = signing_vectors()["payout_ref"][0]
    ref = payout_ref(
        beneficiary_ref=case["beneficiary_ref"],
        fingerprint_hex=case["bank_fingerprint"],
        chain_po_id=case["chain_po_id"],
    )
    assert ref == case["payout_ref"]


@pytest.mark.parametrize(
    "fingerprint",
    [FINGERPRINT.upper(), FINGERPRINT[1:], FINGERPRINT + "00", "0x" + FINGERPRINT[2:]],
)
def test_payout_ref_refuses_a_fingerprint_that_is_not_64_lowercase_hex(fingerprint: str) -> None:
    with pytest.raises(ValueError, match="64 lowercase hex"):
        payout_ref("bnf", fingerprint, "PO-1-7KQ2")


def test_payout_ref_refuses_a_field_longer_than_255_bytes() -> None:
    payout_ref("x" * 255, FINGERPRINT, "PO-1-7KQ2")
    with pytest.raises(ValueError, match="beneficiary_ref"):
        payout_ref("x" * 256, FINGERPRINT, "PO-1-7KQ2")
    with pytest.raises(ValueError, match="chain_po_id"):
        payout_ref("bnf", FINGERPRINT, "é" * 128)


def test_payout_ref_memo_refuses_anything_but_64_lowercase_hex() -> None:
    with pytest.raises(ValueError, match="64 lowercase hex"):
        payout_ref_memo(FINGERPRINT.upper())


# ---- chain_po_id ----


def test_new_chain_po_id_appends_a_crockford_suffix() -> None:
    chain_po_id = new_chain_po_id("PO-12")
    assert re.fullmatch(r"PO-12-[0-9A-HJKMNP-TV-Z]{4}", chain_po_id)
    assert is_chain_po_id(chain_po_id)
    assert CHAIN_PO_ID_RE.fullmatch(chain_po_id)


def test_new_chain_po_id_fits_the_longest_po_ref_in_32_bytes() -> None:
    chain_po_id = new_chain_po_id("PO-".ljust(27, "9"))
    assert len(chain_po_id.encode()) == 32


def test_new_chain_po_id_draws_a_fresh_suffix_from_the_whole_alphabet() -> None:
    ids = [new_chain_po_id("PO-7") for _ in range(2_000)]
    assert len(set(ids)) > 1_900
    assert "".join(sorted({c for i in ids for c in i[-4:]})) == CROCKFORD_BASE32


@pytest.mark.parametrize("po_ref", ["", "po-12", "PO 12", "PO_12", "PO-12\n", "ПО-12", "A" * 28])
def test_new_chain_po_id_refuses_a_ref_that_cannot_make_a_valid_id(po_ref: str) -> None:
    with pytest.raises(ValueError, match="valid chain_po_id"):
        new_chain_po_id(po_ref)


def test_the_service_imports_the_hashes_without_solders() -> None:
    """The service fixes a payment's terms with these; solders loads only with the chain."""
    script = (
        "import sys\n"
        "import bido_back.payments.chain\n"
        "import bido_back.payments.chain.hashes\n"
        "assert not any(name.startswith('solders') for name in sys.modules), 'solders'\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True)  # noqa: S603
