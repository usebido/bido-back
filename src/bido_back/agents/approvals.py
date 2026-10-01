"""Where a run hands off an `act` proposal.

When the model proposes an `act` tool call, the runner never executes it. It
builds an `ApprovalProposal`, passes it to the caller's `ApprovalSink`, emits
`ApprovalRequested`, and stops. The sink is how the caller persists the
proposal (the approvals table is the only authority on what may run); the
runner itself stores nothing.

The sink is awaited BEFORE `ApprovalRequested` is emitted, so a client never
sees a proposal that was not recorded. If the sink raises, the run ends with
`RunFailed` instead.
"""

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ApprovalProposal(BaseModel):
    """Exactly what a person is asked to approve, and the run that asked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    run_id: UUID
    # The caller's session key (not namespaced). Also the key to resume the
    # paused run, together with run_id.
    session_id: str
    tool_call_id: str
    tool: str
    # Canonical arguments: validated by the tool's input model and dumped in
    # JSON mode. `args_hash` is sha256 of their canonical JSON (events.args_hash).
    args: dict[str, JsonValue]
    args_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    # None for background runs acting for the tenant, not a person.
    requested_by: UUID | None


class ApprovalSink(Protocol):
    async def request_approval(self, proposal: ApprovalProposal) -> None: ...


class NoopApprovalSink:
    """Records nothing. For tests and evals, where no one will approve."""

    async def request_approval(self, proposal: ApprovalProposal) -> None:
        return None
