"""Sandbox api shapes."""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel

from bido_back.db.models import SandboxOutbound
from bido_back.simulator.personas import Persona


class PersonaView(BaseModel):
    supplier_id: UUID
    supplier_name: str
    persona: Persona


class OutboundView(BaseModel):
    id: UUID
    invite_id: UUID | None
    subject: str
    to: list[str]
    status: str
    due_at: datetime
    created_at: datetime
    replied_at: datetime | None
    # The simulated answer, when there is one.
    round: int | None
    stance: str | None
    error: str | None

    @classmethod
    def from_row(cls, row: SandboxOutbound) -> Self:
        offer = row.offer or {}
        round_value = offer.get("round")
        stance = offer.get("stance")
        return cls(
            id=row.id,
            invite_id=row.invite_id,
            subject=row.subject,
            to=[str(address) for address in row.to_addresses],
            status=row.status,
            due_at=row.due_at,
            created_at=row.created_at,
            replied_at=row.replied_at,
            round=round_value if isinstance(round_value, int) else None,
            stance=stance if isinstance(stance, str) else None,
            error=row.error,
        )


class ResetResult(BaseModel):
    cancelled: int
    personas_restored: int
