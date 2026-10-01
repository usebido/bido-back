"""`PostgresApprovalSink`: the runner's proposals become `pending` approval rows.

The runner awaits the sink before it emits `ApprovalRequested`, so a client
never sees a proposal that is not in the `approvals` table. The row is written
in the proposal's tenant (forced RLS) and records who asked. The stored args
are read back and must still match `args_hash`, so every pending row is one
the guard can later accept.

Who asked must be a person: `approvals.requested_by` is NOT NULL and the
database forbids `decided_by = requested_by`, which is how "nobody approves
their own request" holds. A background run (`ToolContext.user_id is None`)
therefore cannot request approval yet; the sink refuses with
`ApprovalRequesterMissing` (the run ends with `RunFailed`) instead of
weakening the schema. Background agents need a decision first, e.g. a
per-tenant service user that can request but never approve.
"""

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.events import args_hash
from bido_back.approvals.audit import approval_audit
from bido_back.approvals.errors import (
    ApprovalConflict,
    ApprovalIntegrityError,
    ApprovalRequesterMissing,
)
from bido_back.approvals.integrity import payload_matches
from bido_back.db import tenant_session
from bido_back.db.models import Approval

logger = logging.getLogger(__name__)

_UNIQUE_CALL = "approvals_tenant_id_run_id_tool_call_id_key"


class PostgresApprovalSink:
    def __init__(self, db: AsyncEngine | async_sessionmaker[AsyncSession]) -> None:
        self._db = db

    async def request_approval(self, proposal: ApprovalProposal) -> None:
        requested_by = proposal.requested_by
        if requested_by is None:
            raise ApprovalRequesterMissing(
                "approvals require a requesting user; background runs cannot request "
                "approval until a service-user policy exists"
            )
        if args_hash(proposal.args) != proposal.args_hash:
            raise ApprovalIntegrityError("proposal args_hash does not match its args")

        async with tenant_session(self._db, proposal.tenant_id) as session:
            inserted = await session.scalar(
                insert(Approval)
                .values(
                    tenant_id=proposal.tenant_id,
                    run_id=str(proposal.run_id),
                    session_id=proposal.session_id,
                    tool_call_id=proposal.tool_call_id,
                    tool=proposal.tool,
                    args=proposal.args,
                    args_hash=proposal.args_hash,
                    requested_by=requested_by,
                    status="pending",
                )
                .on_conflict_do_nothing(constraint=_UNIQUE_CALL)
                .returning(Approval.id)
            )
            if inserted is None:
                # The same tool call was recorded before (a retried run). Fine if
                # it is the same proposal; anything else is refused.
                existing = await session.scalar(
                    select(Approval).where(
                        Approval.tenant_id == proposal.tenant_id,
                        Approval.run_id == str(proposal.run_id),
                        Approval.tool_call_id == proposal.tool_call_id,
                    )
                )
                if (
                    existing is None
                    or existing.tool != proposal.tool
                    or existing.args_hash != proposal.args_hash
                    or existing.requested_by != requested_by
                ):
                    raise ApprovalConflict(
                        f"run {proposal.run_id} call {proposal.tool_call_id} "
                        "was already proposed with a different payload"
                    )
                return

            # Read back what jsonb actually stored. If it no longer hashes to the
            # proposal (jsonb rewrites e.g. -0.0 and floats like 1e+20), the
            # payload could never pass the guard: refuse now (rolled back, the
            # run fails) rather than ask a person to approve it.
            approval = await session.get(Approval, inserted)
            if approval is None or not payload_matches(approval):
                raise ApprovalIntegrityError(
                    f"run {proposal.run_id} call {proposal.tool_call_id}: "
                    "args do not survive storage unchanged"
                )
            session.add(approval_audit(approval, "approval.requested", requested_by))
            await session.flush()
        logger.info(
            "approval requested approval=%s tenant=%s run=%s tool=%s",
            inserted,
            proposal.tenant_id,
            proposal.run_id,
            proposal.tool,
        )
