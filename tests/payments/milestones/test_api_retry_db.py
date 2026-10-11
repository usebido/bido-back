"""POST .../milestones/{index}/retry-release: a failed release, released again, once."""

import pytest

from tests.awards.test_awards_db import buyer
from tests.payments.milestones.conftest import Funded
from tests.payments.milestones.support import (
    approve_release,
    milestone_row,
    release_handler,
    release_rows,
    submit_directly,
)
from tests.payments.milestones.test_api_milestones import PO, MilestoneApi, api
from tests.payments.milestones.test_documents_db import outsider

__all__ = ["api"]

pytestmark = pytest.mark.db


async def test_a_failed_release_is_retried_without_a_new_decision(
    funded: Funded, api: MilestoneApi
) -> None:
    submitted = await submit_directly(funded, 0)
    jobs = await approve_release(funded, submitted.approval_id)
    handler = release_handler(funded)
    # The release lands failed and the chain still needs that same transaction.
    funded.chain.next_landing["release"] = "fail"
    assert await handler(jobs[0]) is None
    failed = await milestone_row(funded, 0)
    assert (failed.state, failed.failure_code) == ("failed", "chain_release_failed")

    http = api.client(buyer(funded.people))
    url = f"{PO.format(po=funded.po_id)}/milestones/0/retry-release"
    response = await http.post(url)

    assert response.status_code == 202
    body = response.json()
    assert (body["index"], body["status"], body["failure_code"]) == (0, "releasing", None)
    woken = [job for job in api.jobs.jobs if job.type == "milestone.release"]
    assert [job.idempotency_key for job in woken] == [jobs[0].idempotency_key]

    assert await handler.release(funded.tenant, funded.payment_id, 0) == "released"
    released = await milestone_row(funded, 0)
    assert released.state == "released" and released.release_signature is not None
    # The failed attempt, then the one that landed: the escrow paid milestone 0 once.
    assert await release_rows(funded) == [(0, 1, "failed"), (0, 2, "confirmed")]
    escrow = funded.chain.escrows[(await funded.payment()).escrow_address or ""]
    assert [m.status for m in escrow.milestones] == ["released", "pending", "pending"]

    # Released now: nothing to retry.
    again = await http.post(url)
    assert again.status_code == 409


async def test_only_a_failed_release_of_your_company_is_retried(
    funded: Funded, api: MilestoneApi
) -> None:
    await submit_directly(funded, 0)
    url = f"{PO.format(po=funded.po_id)}/milestones/0/retry-release"

    under_review = await api.client(buyer(funded.people)).post(url)
    other_company = await api.client(outsider(funded)).post(url)

    assert under_review.status_code == 409
    assert other_company.status_code == 404
    assert (await milestone_row(funded, 0)).state == "submitted"
    assert not [job for job in api.jobs.jobs if job.type == "milestone.release"]
