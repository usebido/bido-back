"""Who would receive an email: classify each recipient's domain before Bido sends.

A buyer's spreadsheet can hold made-up supplier addresses (e.g. generated for
a demo) whose domains belong to real companies. Before anything goes out from
the buyer's mailbox, each recipient is classified:

- `test`: a reserved domain (RFC 2606 / 6761: `example.com|net|org`, any
  `.example`, `.test`, `.invalid`, `.localhost`, and `localhost`). Nothing
  real can receive it.
- `real_domain`: the domain publishes MX records, so a real mailbox may get it.
- `no_mail`: the domain does not exist or publishes no MX.
- `unknown`: DNS did not answer in time (or failed); treated conservatively.
- `invalid`: not an email address.

The classification is for people (what the approval shows). What may actually
be sent is decided in `bido_back.outbound.domains` from the domain alone (test
domains pass; any other needs the buyer's confirmation), so a slow or failing
DNS never lets an email through.
"""

import asyncio
import functools
import logging
import re
import time
from typing import Final, Literal, Protocol

logger = logging.getLogger(__name__)

RecipientStatus = Literal["test", "real_domain", "no_mail", "unknown", "invalid"]

_RESERVED_EXACT: Final = frozenset(
    {"example.com", "example.net", "example.org", "localhost", "example", "test", "invalid"}
)
_RESERVED_SUFFIXES: Final = (
    ".example.com",
    ".example.net",
    ".example.org",
    ".example",
    ".test",
    ".invalid",
    ".localhost",
)
# A pragmatic shape check; addresses were validated when the contact was stored.
_ADDRESS: Final = re.compile(r"^[^@\s]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)$")


def domain_of(address: str) -> str | None:
    """The lower-cased domain of an address, or None if it is not one."""
    match = _ADDRESS.match(address.strip())
    return match.group(1).lower().rstrip(".") if match else None


def is_test_domain(domain: str) -> bool:
    d = domain.lower().rstrip(".")
    return d in _RESERVED_EXACT or d.endswith(_RESERVED_SUFFIXES)


# Free/consumer mailbox providers: anyone can own an address there, so a
# confirmation never covers the whole domain, only the exact address. The one
# list of them; `yahoo` and `gmx` match every country domain (yahoo.com.br,
# gmx.de, ...).
_CONSUMER_EXACT: Final = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "outlook.com.br",
        "hotmail.com",
        "hotmail.com.br",
        "live.com",
        "msn.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "zoho.com",
        "yandex.com",
        "mail.com",
        "uol.com.br",
        "bol.com.br",
        "terra.com.br",
        "ig.com.br",
    }
)
_CONSUMER_LABELS: Final = ("yahoo", "ymail", "gmx", "hotmail", "outlook", "live")


def is_consumer_domain(domain: str) -> bool:
    """A free mailbox provider (gmail.com, outlook.com, yahoo.*, ...), not a company."""
    d = domain.lower().rstrip(".")
    if d in _CONSUMER_EXACT:
        return True
    # yahoo.com.br, gmx.de, hotmail.fr, live.co.uk: the provider label comes
    # first and only country/generic suffixes follow.
    first, _, rest = d.partition(".")
    return first in _CONSUMER_LABELS and bool(rest) and len(rest) <= 6


def confirmation_key(address: str) -> str | None:
    """What a person confirms to let email go to `address`: its domain, or for a
    consumer provider the exact (lower-cased) address. None if not an address."""
    domain = domain_of(address)
    if domain is None:
        return None
    if is_consumer_domain(domain):
        return address.strip().lower()
    return domain


class MxResolver(Protocol):
    async def has_mx(self, domain: str) -> bool | None:
        """True: publishes MX. False: no such domain, or no MX. None: unknown."""
        ...


class DnsMxResolver:
    """MX lookups with dnspython: bounded by `timeout`, cached for `ttl` seconds."""

    def __init__(self, timeout: float = 2.0, ttl: float = 3600.0) -> None:
        self._timeout = timeout
        self._ttl = ttl
        self._cache: dict[str, tuple[bool | None, float]] = {}

    async def has_mx(self, domain: str) -> bool | None:
        now = time.monotonic()
        cached = self._cache.get(domain)
        if cached is not None and cached[1] > now:
            return cached[0]
        result = await self._lookup(domain)
        # Unknown answers are not cached for long: DNS may be back soon.
        ttl = self._ttl if result is not None else 60.0
        self._cache[domain] = (result, now + ttl)
        return result

    async def _lookup(self, domain: str) -> bool | None:
        import dns.asyncresolver
        import dns.exception
        import dns.resolver

        resolver = dns.asyncresolver.Resolver()
        resolver.lifetime = self._timeout
        try:
            answer = await asyncio.wait_for(resolver.resolve(domain, "MX"), self._timeout + 0.5)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return False
        except (dns.exception.DNSException, TimeoutError, OSError):
            logger.info("mx lookup failed for a recipient domain; treated as unknown")
            return None
        return len(answer) > 0


@functools.cache
def default_resolver() -> DnsMxResolver:
    """One cached resolver per process."""
    return DnsMxResolver()


async def classify(address: str, resolver: MxResolver) -> RecipientStatus:
    domain = domain_of(address)
    if domain is None:
        return "invalid"
    if is_test_domain(domain):
        return "test"
    has_mx = await resolver.has_mx(domain)
    if has_mx is None:
        return "unknown"
    return "real_domain" if has_mx else "no_mail"
