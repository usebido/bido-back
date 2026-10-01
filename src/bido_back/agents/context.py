"""The identity every tool call runs under.

The runner builds one `ToolContext` per run and passes it, unchanged, to every
tool handler. Tools read the tenant from here, never from model-supplied
arguments: the model can name things, it cannot choose whose data it touches.
"""

from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ToolContext:
    tenant_id: UUID
    # None for background jobs that act on behalf of the tenant, not a person.
    user_id: UUID | None
    run_id: UUID
    roles: frozenset[str] = field(default_factory=frozenset[str])

    def has_role(self, role: str) -> bool:
        return role in self.roles
