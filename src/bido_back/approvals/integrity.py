"""The payload check shared by every step of the guard.

The hash is recomputed with the SAME function the runner used when it
proposed the call (`events.args_hash`), over the arguments as stored in
`approvals.args`. Postgres `jsonb` normalizes key order and whitespace, which
canonical JSON ignores anyway; a value whose JSON spelling jsonb changes (for
example a float written in exponent form) makes the hashes differ, and the
guard then refuses: a false alarm is safe, a silent mismatch is not.

`approved_payload` is what the executor hashes again after the tool parsed
the stored arguments: an optional field the stored arguments do not carry
(added to the tool after the proposal) is left out, so adding one never breaks
an approval waiting at deploy time.
"""

import hmac
from collections.abc import Mapping, Sequence
from typing import cast

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from bido_back.agents.events import args_hash
from bido_back.db.models import Approval

_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def approved_payload(args: BaseModel) -> dict[str, JsonValue]:
    """Parsed tool arguments as JSON, in the shape the approval stored them.

    A field the stored arguments did not carry and that is None (an optional
    field added to the tool after the proposal) is left out, in nested models
    too: it adds nothing to what runs. A new field with any other default stays
    in and changes the hash, so the executor refuses: what would run is no
    longer what the approver saw.
    """
    return _without_absent(args, cast(dict[str, JsonValue], args.model_dump(mode="json")))


def _without_absent(model: BaseModel, dumped: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    fields = type(model).model_fields
    kept: dict[str, JsonValue] = {}
    for name, value in dumped.items():
        current: object = getattr(model, name, None)
        if name in fields and name not in model.model_fields_set and current is None:
            continue
        kept[name] = _nested(current, value)
    return kept


def _nested(current: object, dumped: JsonValue) -> JsonValue:
    if isinstance(current, BaseModel) and isinstance(dumped, dict):
        return _without_absent(current, dumped)
    if isinstance(current, list | tuple) and isinstance(dumped, list):
        items = cast(Sequence[object], current)
        if len(items) == len(dumped):
            return [_nested(item, value) for item, value in zip(items, dumped, strict=True)]
    return dumped


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
