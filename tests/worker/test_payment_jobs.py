"""On servers with payments the worker registers what moves money: the act tools and the
chain jobs, milestone releases included (`build_worker_registry` uses `payment_handlers`)."""

from sqlalchemy.ext.asyncio import create_async_engine

from bido_back.approvals.executor import APPROVAL_EXECUTE_JOB, ActToolRegistry
from bido_back.payments.milestones.proposals import RELEASE_PO_MILESTONE
from bido_back.payments.milestones.release import MilestoneReleaseHandler
from bido_back.payments.pipeline import PaymentAdvanceHandler
from bido_back.payments.proposals import PAY_PURCHASE_ORDER
from bido_back.settings import Settings
from bido_back.worker.job_types import MILESTONE_RELEASE, PAYMENT_ADVANCE
from bido_back.worker.main import build_registry, payment_handlers
from tests.payments.fakes import FakeProviders

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"


async def test_payments_register_the_milestone_release_tool_and_job() -> None:
    settings = Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    engine = create_async_engine(UNUSED_DB)  # never connected here
    act_tools = ActToolRegistry()
    try:
        handlers = payment_handlers(
            act_tools, engine, FakeProviders().bundle(), None, settings=settings
        )
        registry = build_registry(engine, act_tools)
        for job_type, handler in handlers.items():
            registry.register(job_type, handler)
    finally:
        await engine.dispose()

    assert sorted(handlers) == sorted([PAYMENT_ADVANCE, MILESTONE_RELEASE])
    assert isinstance(registry.get(MILESTONE_RELEASE), MilestoneReleaseHandler)
    assert isinstance(registry.get(PAYMENT_ADVANCE), PaymentAdvanceHandler)
    assert registry.get(APPROVAL_EXECUTE_JOB) is not None
    # The approved release runs only as an act tool of the approval executor.
    for name in (RELEASE_PO_MILESTONE, PAY_PURCHASE_ORDER):
        tool = act_tools.get(name)
        assert tool is not None and tool.kind == "act"
