"""The job envelope: the JSON body of every worker message.

    {"type": "...", "tenant_id": "<uuid>", "idempotency_key": "...", "payload": {...}}

It lives in its own module so producers (the api, the approval guard) can
build envelopes without importing the worker's runtime.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Envelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str = Field(min_length=1)
    tenant_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    payload: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
