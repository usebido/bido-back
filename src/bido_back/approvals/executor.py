"""Executing an approved `act` tool: the worker's `approval.execute` job.

This is the ONLY code path that runs an act tool. Agno never executes one
(the adapter registers them with `external_execution`), and approving does
not execute inline: `decide_approval` enqueues this job after its commit.

Per job, inside one transaction in the job's tenant:

1. `SELECT ... FOR UPDATE` the approval. A redelivered or duplicated job
   waits here for the first one to finish.
2. `executed` -> no-op (idempotent). Anything but `approved` -> refused.
3. The stored args must still hash to `args_hash`, and the tool's input
   model must reproduce exactly those args, so what runs is byte-for-byte
   what the approver saw.
4. The tool's handler runs with a `ToolContext` built from the approval:
   the approval's tenant, and the APPROVER (`decided_by`) as the acting user,
   because the approver is the person who authorized the commit (the
   requester stays on record in the row and the audit trail).
5. The row becomes `executed` and an audit event is written; both commit
   together.

Known gap until the outbox exists: the handler runs while the row lock is
held, so concurrent duplicates cannot both run it, but a crash after the
handler's external effect and before the commit leaves the row `approved`
and the job is retried. Act handlers must therefore be idempotent on their
own (keyed by the approval) or, better, write an outbox row instead of
calling a provider directly.

The paused agent run is not continued here: the guard never calls Agno's
continue flow.
"""

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from pydantic import JsonValue, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.context import ToolContext
from bido_back.agents.events import args_hash
from bido_back.agents.tools import AnyTool
from bido_back.approvals.audit import approval_audit
from bido_back.approvals.errors import (
    ApprovalError,
    ApprovalIntegrityError,
    ApprovalNotExecutable,
    ApprovalNotFound,
    InvalidApprovalJob,
    UnknownActTool,
)
from bido_back.approvals.integrity import payload_matches
from bido_back.db import tenant_session
from bido_back.db.models import Approval
from bido_back.worker.envelope import Envelope

logger = logging.getLogger(__name__)

APPROVAL_EXECUTE_JOB: Final = "approval.execute"


def approval_job_key(approval_id: UUID) -> str:
    """The idempotency key of an approval's execution job: one per approval."""
    return f"approval:{approval_id}"


def approval_execute_envelope(tenant_id: UUID, approval_id: UUID) -> Envelope:
    return Envelope(
        type=APPROVAL_EXECUTE_JOB,
        tenant_id=tenant_id,
        idempotency_key=approval_job_key(approval_id),
        payload={"approval_id": str(approval_id)},
    )


class ActToolRegistry:
    """The act tools this process can execute, by name. Empty by default."""

    def __init__(self, tools: Iterable[AnyTool] = ()) -> None:
        self._tools: dict[str, AnyTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AnyTool) -> None:
        if tool.kind != "act":
            raise ValueError(f"tool {tool.name!r} is a {tool.kind!r} tool, not an act tool")
        if tool.name in self._tools:
            raise ValueError(f"act tool already registered: {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AnyTool | None:
        return self._tools.get(name)


def _approval_id(envelope: Envelope) -> UUID:
    raw: JsonValue = envelope.payload.get("approval_id")
    if set(envelope.payload) != {"approval_id"} or not isinstance(raw, str):
        raise InvalidApprovalJob("payload must be exactly {approval_id}")
    try:
        approval_id = UUID(raw)
    except ValueError:
        raise InvalidApprovalJob("approval_id is not a UUID") from None
    if envelope.idempotency_key != approval_job_key(approval_id):
        raise InvalidApprovalJob("idempotency_key does not match the approval")
    return approval_id


class ApprovalExecutor:
    """The `approval.execute` job handler."""

    def __init__(
        self, db: AsyncEngine | async_sessionmaker[AsyncSession], tools: ActToolRegistry
    ) -> None:
        self._db = db
        self._tools = tools

    async def __call__(self, envelope: Envelope) -> None:
        approval_id = _approval_id(envelope)
        refused: ApprovalError | None = None

        async with tenant_session(self._db, envelope.tenant_id) as session:
            approval = await session.scalar(
                select(Approval).where(Approval.id == approval_id).with_for_update()
            )
            if approval is None or approval.tenant_id != envelope.tenant_id:
                raise ApprovalNotFound(f"approval {approval_id} not found in tenant")
            if approval.status == "executed":
                logger.info("approval %s already executed; job is a no-op", approval_id)
                return
            if approval.status != "approved" or approval.decided_by is None:
                raise ApprovalNotExecutable(f"approval {approval_id} is {approval.status}")

            if not payload_matches(approval):
                # Recorded and committed; the job then fails (and is retried
                # until the dead-letter queue, which is where a person looks).
                session.add(
                    approval_audit(approval, "approval.integrity_failed", approval.decided_by)
                )
                refused = ApprovalIntegrityError(f"approval {approval_id}: args do not match hash")
            else:
                await self._execute(session, approval, approval.decided_by)

        if refused is not None:
            logger.error("refused to execute approval %s: integrity check failed", approval_id)
            raise refused

    async def _execute(self, session: AsyncSession, approval: Approval, actor: UUID) -> None:
        tool = self._tools.get(approval.tool)
        if tool is None:
            raise UnknownActTool(f"approval {approval.id}: no act tool named {approval.tool!r}")
        try:
            args = tool.parse_input(approval.args)
        except ValidationError:
            # Never chain the ValidationError: its text includes the input values.
            raise ApprovalNotExecutable(
                f"approval {approval.id}: stored args no longer validate for {tool.name!r}"
            ) from None
        if args_hash(args.model_dump(mode="json")) != approval.args_hash:
            raise ApprovalNotExecutable(
                f"approval {approval.id}: {tool.name!r} no longer reproduces the approved args"
            )
        try:
            run_id = UUID(approval.run_id)
        except ValueError:
            raise ApprovalNotExecutable(f"approval {approval.id}: run_id is not a UUID") from None

        context = ToolContext(tenant_id=approval.tenant_id, user_id=actor, run_id=run_id)
        await tool.invoke(context, args)

        approval.status = "executed"
        approval.executed_at = datetime.now(UTC)
        session.add(approval_audit(approval, "approval.executed", actor))
        await session.flush()
        logger.info(
            "executed approval %s tenant=%s tool=%s", approval.id, approval.tenant_id, tool.name
        )
