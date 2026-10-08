"""Recipient classification: reserved test domains, MX lookups, unknown answers."""

import pytest
from pydantic import JsonValue

from bido_back.email.recipients import (
    DnsMxResolver,
    classify,
    domain_of,
    is_test_domain,
)
from bido_back.outbound.domains import normalize_domain, recipients_in_args


class StubResolver:
    def __init__(self, answers: dict[str, bool | None]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    async def has_mx(self, domain: str) -> bool | None:
        self.asked.append(domain)
        return self.answers.get(domain)


def test_domain_of() -> None:
    assert domain_of("Ana@Acme.COM") == "acme.com"
    assert domain_of(" sales@rame-italia.it ") == "rame-italia.it"
    assert domain_of("not an address") is None
    assert domain_of("x@localhost") is None


@pytest.mark.parametrize(
    "domain",
    ["example.com", "sub.example.org", "acme.example", "supplier.test", "x.invalid", "a.localhost"],
)
def test_reserved_domains_are_test_domains(domain: str) -> None:
    assert is_test_domain(domain)


@pytest.mark.parametrize("domain", ["acme.com", "example-motors.com", "test.it", "mytest.com"])
def test_other_domains_are_not(domain: str) -> None:
    assert not is_test_domain(domain)


async def test_classify() -> None:
    resolver = StubResolver({"acme.com": True, "nomail.com": False, "slow.com": None})

    assert await classify("a@supplier.test", resolver) == "test"
    assert await classify("a@acme.com", resolver) == "real_domain"
    assert await classify("a@nomail.com", resolver) == "no_mail"
    assert await classify("a@slow.com", resolver) == "unknown"
    assert await classify("nope", resolver) == "invalid"
    # Test domains never hit DNS.
    assert "supplier.test" not in resolver.asked


async def test_dns_resolver_caches_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = DnsMxResolver()
    calls: list[str] = []

    async def lookup(domain: str) -> bool | None:
        calls.append(domain)
        return True

    monkeypatch.setattr(resolver, "_lookup", lookup)

    assert await resolver.has_mx("acme.com") is True
    assert await resolver.has_mx("acme.com") is True
    assert calls == ["acme.com"]


def test_recipients_in_args() -> None:
    assert recipients_in_args({"to": ["a@x.com"], "cc": ["b@y.com", "a@x.com"]}) == [
        "a@x.com",
        "b@y.com",
    ]
    mandate: dict[str, JsonValue] = {
        "suppliers": [{"to": ["s@rame.it"]}, {"to": ["t@acc.it", "s@rame.it"]}]
    }
    assert recipients_in_args(mandate) == ["s@rame.it", "t@acc.it"]
    assert recipients_in_args({"supplier_id": "x", "amount_cents": 1}) == []


def test_normalize_domain() -> None:
    assert normalize_domain(" @Acme.COM. ") == "acme.com"
    assert normalize_domain("a@b.com") is None
    assert normalize_domain("nodot") is None
