"""The payload check shared by every step of the guard.

The hash is recomputed with the SAME function the runner used when it
proposed the call (`events.args_hash`), over the arguments as stored in
`approvals.args`. Postgres `jsonb` normalizes key order and whitespace, which
canonical JSON ignores anyway; a value whose JSON spelling jsonb changes (for
example a float written in exponent form) makes the hashes differ, and the
guard then refuses: a false alarm is safe, a silent mismatch is not.
"""

import hmac

from pydantic import JsonValue, TypeAdapter, ValidationError

from bido_back.agents.events import args_hash
from bido_back.db.models import Approval

_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def stored_args(approval: Approval) -> dict[str, JsonValue] | None:
    """The stored arguments as a JSON object, or None if they are not one."""
    try:
        return _JSON_OBJECT.validate_python(approval.args)
    except ValidationError:
        return None


def payload_matches(approval: Approval) -> bool:
    args = stored_args(approval)
    if args is None:
        return False
    return hmac.compare_digest(args_hash(args), approval.args_hash)
