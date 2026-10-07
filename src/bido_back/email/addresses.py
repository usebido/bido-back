"""Per-invite reply addresses, RFQ subject markers and Message-IDs.

Every invite gets its own unguessable reply address,
`rfq-<reply_token>@<email_domain>`, used as From and Reply-To on the RFQ. A
supplier's reply comes back to that address, and the token alone identifies
the invite (and so the tenant) before anything else is known about the mail.

The subject also carries an `[RFQ-<n>]` marker (`rfq_marker(ref)`), a weaker
fallback for replies that lost both the reply address and the threading
headers. A ref is guessable and unique only per tenant, so it is only ever
used together with a known sender (see migration 0004).
"""

import re
import secrets
import uuid
from collections.abc import Iterable
from email.utils import getaddresses

# RFC 4648 base32 alphabet, lowercased: no 0/1/8/9, so no 0-vs-o or 1-vs-l
# confusion when a person retypes an address.
_BASE32_LOWER = "abcdefghijklmnopqrstuvwxyz234567"
TOKEN_LENGTH = 12  # 60 bits of entropy
_LOCAL_PREFIX = "rfq-"
_TOKEN_RE = re.compile(rf"^rfq-([{_BASE32_LOWER}]{{{TOKEN_LENGTH}}})$")
# `[RFQ-12]`; tolerates a doubled prefix (`[RFQ-RFQ-12]`) and spaces inside the brackets.
_MARKER_RE = re.compile(r"\[\s*(?:RFQ-)?(RFQ-[A-Z0-9][A-Z0-9._/-]{0,59})\s*\]", re.IGNORECASE)
_MAX_MARKERS = 5


def new_reply_token() -> str:
    """A fresh 12-character token from the OS CSPRNG."""
    return "".join(secrets.choice(_BASE32_LOWER) for _ in range(TOKEN_LENGTH))


def reply_address(token: str, domain: str) -> str:
    if not is_reply_token(token):
        raise ValueError("not a reply token")
    return f"{_LOCAL_PREFIX}{token}@{domain.lower()}"


def is_reply_token(token: str) -> bool:
    return _TOKEN_RE.match(f"{_LOCAL_PREFIX}{token}") is not None


def extract_reply_tokens(addresses: Iterable[str], domain: str) -> list[str]:
    """Reply tokens found in `addresses` for our `domain`, in order, deduplicated.

    Accepts bare addresses or header values with display names and several
    comma-separated addresses ("Bido <rfq-abc...@quotes.example>, x@y").
    Matching is case-insensitive (mail systems may change case); a `+tag`
    subaddress after the token is ignored.
    """
    wanted_domain = domain.lower()
    tokens: list[str] = []
    for _, address in getaddresses(list(addresses)):
        local, sep, host = address.strip().lower().rpartition("@")
        if not sep or host != wanted_domain:
            continue
        local = local.split("+", 1)[0]
        match = _TOKEN_RE.match(local)
        if match and match.group(1) not in tokens:
            tokens.append(match.group(1))
    return tokens


def rfq_marker(ref: str) -> str:
    """The subject marker for an RFQ ref: `[RFQ-12]` for ref "RFQ-12" (or "12")."""
    ref = ref.strip().upper()
    return f"[{ref}]" if ref.startswith("RFQ-") else f"[RFQ-{ref}]"


def extract_rfq_refs(*texts: str | None) -> list[str]:
    """RFQ refs from `[RFQ-<n>]` markers in `texts` (e.g. subject, then body), in order.

    Uppercased and deduplicated; at most a handful, so a body quoting many
    RFQs cannot turn one email into many lookups.
    """
    refs: list[str] = []
    for text in texts:
        for match in _MARKER_RE.finditer(text or ""):
            ref = match.group(1).upper()
            if ref not in refs:
                refs.append(ref)
            if len(refs) >= _MAX_MARKERS:
                return refs
    return refs


def make_message_id(domain: str) -> str:
    """A globally unique Message-ID, angle brackets included."""
    return f"<{uuid.uuid4()}@{domain.lower()}>"


def address_domain(address: str) -> str | None:
    """The lowercased domain of an email address, or None if it has none."""
    _, sep, host = address.strip().rpartition("@")
    return host.lower() if sep and host else None
