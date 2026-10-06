"""Reply tokens, reply addresses and Message-IDs."""

import re

import pytest

from bido_back.email.addresses import (
    TOKEN_LENGTH,
    extract_reply_tokens,
    is_reply_token,
    make_message_id,
    new_reply_token,
    reply_address,
)

DOMAIN = "quotes.example.test"


def test_new_reply_token_is_12_lowercase_base32_chars() -> None:
    tokens = {new_reply_token() for _ in range(200)}
    assert len(tokens) == 200
    for token in tokens:
        assert len(token) == TOKEN_LENGTH
        assert re.fullmatch(r"[a-z2-7]{12}", token)
        assert is_reply_token(token)


def test_reply_address_round_trips_through_extraction() -> None:
    token = new_reply_token()
    address = reply_address(token, DOMAIN.upper())
    assert address == f"rfq-{token}@{DOMAIN}"
    assert extract_reply_tokens([address], DOMAIN) == [token]


def test_reply_address_rejects_non_tokens() -> None:
    with pytest.raises(ValueError):
        reply_address("../etc", DOMAIN)


def test_extract_handles_display_names_lists_case_and_subaddress() -> None:
    a, b = new_reply_token(), new_reply_token()
    headers = [
        f'"Bido RFQ" <RFQ-{a.upper()}@{DOMAIN.upper()}>, buyer@acme.test',
        f"rfq-{b}+thread@{DOMAIN}",
        f"rfq-{a}@{DOMAIN}",  # duplicate
    ]
    assert extract_reply_tokens(headers, DOMAIN) == [a, b]


def test_extract_ignores_other_domains_and_malformed_tokens() -> None:
    token = new_reply_token()
    headers = [
        f"rfq-{token}@evil.test",
        f"rfq-{token}@sub.{DOMAIN}",
        f"rfq-{token[:-1]}@{DOMAIN}",
        f"rfq-{token}0@{DOMAIN}",
        "not an address",
    ]
    assert extract_reply_tokens(headers, DOMAIN) == []


def test_make_message_id_is_unique_and_bracketed() -> None:
    ids = {make_message_id(DOMAIN) for _ in range(50)}
    assert len(ids) == 50
    for message_id in ids:
        assert re.fullmatch(rf"<[0-9a-f-]{{36}}@{re.escape(DOMAIN)}>", message_id)
