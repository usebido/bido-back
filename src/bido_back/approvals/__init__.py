"""The approval guard: how an `act` tool call gets from proposal to execution.

    runner --ApprovalProposal--> PostgresApprovalSink   (pending row)      sink.py
    approver --POST decision---> decide_approval        (approved/rejected) guard.py
    worker --approval.execute--> ApprovalExecutor       (executed)          executor.py

Each step re-checks the stored payload against its hash, in the tenant's
RLS-scoped transaction, under a row lock.
"""

from bido_back.approvals.errors import (
    ApprovalConflict,
    ApprovalError,
    ApprovalForbidden,
    ApprovalIntegrityError,
    ApprovalNotExecutable,
    ApprovalNotFound,
    ApprovalNotPending,
    ApprovalRequesterMissing,
    InvalidApprovalJob,
    UnknownActTool,
)
from bido_back.approvals.executor import (
    APPROVAL_EXECUTE_JOB,
    ActToolRegistry,
    ApprovalExecutor,
    approval_execute_envelope,
    approval_job_key,
)
from bido_back.approvals.guard import (
    APPROVER_ROLES,
    Decision,
    DecisionOutcome,
    decide_approval,
)
from bido_back.approvals.sink import PostgresApprovalSink

__all__ = [
    "APPROVAL_EXECUTE_JOB",
    "APPROVER_ROLES",
    "ActToolRegistry",
    "ApprovalConflict",
    "ApprovalError",
    "ApprovalExecutor",
    "ApprovalForbidden",
    "ApprovalIntegrityError",
    "ApprovalNotExecutable",
    "ApprovalNotFound",
    "ApprovalNotPending",
    "ApprovalRequesterMissing",
    "Decision",
    "DecisionOutcome",
    "InvalidApprovalJob",
    "PostgresApprovalSink",
    "UnknownActTool",
    "approval_execute_envelope",
    "approval_job_key",
    "decide_approval",
]
