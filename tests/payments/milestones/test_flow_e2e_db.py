"""A tooling purchase order paid milestone by milestone, end to end.

The production code path throughout, with the providers faked (`tests/payments/fakes.py`):
the buyer asks to pay a sent PO (Bido's tooling plan by default: Tool start 30%, T1 30%,
PPAP 40%) and approves it; the worker charges the card once and funds the escrow. The
buyer gives the factory its link; the factory confirms the order and, milestone by
milestone, sends the documents the catalog requires; a person approves each release from
the approval queue; the worker claims it and the release job writes the gate's
attestation and releases that milestone, once. T1 is sent back once (changes requested)
and sent again with a corrected report. Three releases later the payment is complete
(`completed_at`; it stays `funded`).
"""

import io
import secrets
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import pytest
from sqlalchemy import select

from bido_back.approvals import decide_approval
from bido_back.auth import Principal
from bido_back.awards import service as award_service
from bido_back.awards.send import AWARD_REJECT_HOOKS
from bido_back.db import tenant_session
from bido_back.db.models import Approval, AuditEvent, IssuedPurchaseOrder
from bido_back.payments import service
from bido_back.payments.execute import (
    PAYMENT_REJECT_HOOKS,
    PaymentActDeps,
    register_payment_act_tools,
)
from bido_back.payments.milestones import service as milestones
from bido_back.payments.milestones.catalog import definition_for
from bido_back.payments.milestones.documents import add_document, remove_document
from bido_back.payments.milestones.execute import MilestoneActDeps, register_milestone_act_tools
from bido_back.payments.milestones.hooks import MILESTONE_REJECT_HOOKS
from bido_back.payments.milestones.links import (
    FactoryAccess,
    accept_order,
    issue_factory_link,
    resolve_factory_link,
)
from bido_back.payments.milestones.proposals import MILESTONE_RELEASE, RELEASE_PO_MILESTONE
from bido_back.payments.milestones.release import MilestoneReleaseHandler
from bido_back.payments.milestones.schemas import ReleaseMilestoneArgs, SubmitMilestoneRequest
from bido_back.payments.pipeline import PaymentAdvanceHandler
from bido_back.payments.proposals import PAYMENT_ADVANCE
from bido_back.payments.schemas import PayPurchaseOrderRequest
from bido_back.payments.views import PurchaseOrderPaymentSummary
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs.activity import rfq_activity
from bido_back.settings import Settings
from bido_back.storage import BlobNotFound
from tests.approvals.conftest import FakeJobQueue
from tests.awards.test_awards_db import World, approver, buyer
from tests.awards.test_lifecycle import sent_po
from tests.payments.fakes import FakeProviders, address
from tests.payments.flow.conftest import chain_transactions
from tests.payments.flow.test_end_to_end_db import ACCOUNT
from tests.payments.flow.test_service_db import RATE_DAY, card_token, usd_rate

__all__ = ["usd_rate"]

pytestmark = pytest.mark.db

SETTINGS = Settings.model_validate(
    {
        "environment": "test",
        "database_url": "postgresql+asyncpg://app@localhost:5432/unused",
        "basis_theory_api_key": "test",
        "privy_app_id": "test",
        "privy_app_secret": "test",
        "privy_authorization_key": "test",
        "privy_wallet_owner_id": "test",
        "solana_rpc_url": "http://solana.invalid",
        "solana_stablecoin_mint": address("mint"),
        "solana_mint_authority_key": "test",
        "solana_operator_key": "test",
        "solana_relayer_key": "test",
        "solana_payout_owner": address("payout"),
        "solana_gate_attester": address("gate-attester"),
        "solana_gate_attester_key": "test",
        "payout_details_encryption_key": "test",
        "payments_card_charge": "simulated",
        "factory_page_url": "https://app.example.test/factory",
    }
)
PLAN = [("tool_start", "Tool start", 3_000), ("t1", "T1", 3_000), ("ppap", "PPAP", 4_000)]
REJECTS = {**AWARD_REJECT_HOOKS, **PAYMENT_REJECT_HOOKS, **MILESTONE_REJECT_HOOKS}
REASON = "Cavity 2 measures 0.3 mm short on the clip"


class MemoryStorage:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.blobs[key] = data

    async def get(self, key: str) -> bytes:
        try:
            return self.blobs[key]
        except KeyError:
            raise BlobNotFound(key) from None


def sample(kind: str) -> tuple[str, bytes]:
    """A tiny valid file of `kind` (its extension and bytes), unique so never a repeat."""
    noise = secrets.token_bytes(32)
    match kind:
        case "pdf":
            return "pdf", b"%PDF-1.4\n%" + noise + b"\n%%EOF\n"
        case "png":
            return "png", b"\x89PNG\r\n\x1a\n" + noise
        case "jpg" | "jpeg":
            return "jpg", b"\xff\xd8\xff\xe0" + noise
        case "webp":
            return "webp", b"RIFF" + (len(noise) + 4).to_bytes(4, "little") + b"WEBP" + noise
        case "xlsx":
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("xl/workbook.xml", "<workbook/>")
                archive.writestr("xl/noise.bin", noise)
            return "xlsx", buffer.getvalue()
        case _:
            raise AssertionError(f"no sample file of kind {kind}")


@dataclass
class Tooling:
    """A sent PO paid on the tooling plan, the factory's access, and the worker's queues."""

    world: World
    providers: FakeProviders
    po: IssuedPurchaseOrder
    payment_id: UUID
    access: FactoryAccess
    storage: MemoryStorage = field(default_factory=MemoryStorage)
    releases: FakeJobQueue = field(default_factory=FakeJobQueue)
    # (milestone index, the approval's arguments) of every release approved, in order.
    approved: list[tuple[int, ReleaseMilestoneArgs]] = field(
        default_factory=list[tuple[int, ReleaseMilestoneArgs]]
    )

    @property
    def owner(self) -> Principal:
        return buyer(self.world.people)

    @property
    def approver(self) -> Principal:
        return approver(self.world.people)

    async def upload(self, index: int, code: str, *, version: int = 1) -> str:
        """The factory adds one file of document type `code`; returns its sha256."""
        key, name, bps = PLAN[index]
        document = definition_for(key, name, bps).document(code)
        assert document is not None
        extension, data = sample(document.accepts[0])
        view = await add_document(
            self.world.engine,
            self.storage,
            self.access,
            self.payment_id,
            index,
            doc_type=code,
            filename=f"{code}-v{version}.{extension}",
            content_type="application/octet-stream",
            data=data,
            settings=SETTINGS,
        )
        assert (view.doc_type, view.source, view.size_bytes) == (code, "factory", len(data))
        return view.sha256

    async def upload_required(self, index: int) -> list[str]:
        """Every document type milestone `index` requires, one file each (catalog order)."""
        key, name, bps = PLAN[index]
        definition = definition_for(key, name, bps)
        required = [d.code for d in definition.documents if d.required]
        assert set(required) == definition.required_codes and required
        return [await self.upload(index, code) for code in required]

    async def submit(self, index: int, note: str) -> UUID:
        """The factory sends milestone `index` for approval; returns the approval's id."""
        submitted = await milestones.submit_milestone(
            self.world.engine,
            self.access,
            self.payment_id,
            index,
            SubmitMilestoneRequest(note=note),
            settings=SETTINGS,
        )
        assert (submitted.milestone.index, submitted.milestone.status) == (index, "in_review")
        return submitted.approval_id

    async def queued(self, approval_id: UUID) -> ReleaseMilestoneArgs:
        """The release in the approval queue, as the approver sees it; its arguments."""
        queue = await rfq_service.list_approvals(self.world.engine, self.approver, "pending")
        [item] = [i for i in queue if i.id == approval_id]
        assert (item.tool, item.requested_by) == (RELEASE_PO_MILESTONE, self.world.people.owner)
        view = item.milestone_release
        assert view is not None and item.payment is None and item.recipient_checks is None
        async with tenant_session(self.world.engine, self.world.people.tenant) as session:
            approval = await session.get(Approval, approval_id)
            assert approval is not None
            args = ReleaseMilestoneArgs.model_validate(approval.args)
        key, name, bps = PLAN[args.milestone_index]
        definition = definition_for(key, name, bps)
        assert (view.milestone_key, view.milestone_name, view.label, view.bps) == (
            key,
            name,
            definition.label,
            bps,
        )
        assert (view.purchase_order_ref, view.supplier_name, view.source) == (
            self.po.ref,
            "Rhein",
            "factory",
        )
        assert (view.evidence_hash, view.attempt) == (args.evidence_hash, args.attempt)
        assert [(d.document_id, d.sha256) for d in view.documents] == [
            (d.document_id, d.sha256) for d in args.documents
        ]
        assert all(d.label != d.doc_type for d in view.documents)
        assert view.checks == list(definition.checks)
        return args

    async def approve_and_release(self, approval_id: UUID) -> None:
        """A person approves; the worker runs the act, then the release job (each twice:
        queues redeliver)."""
        args = await self.queued(approval_id)
        decision = FakeJobQueue()
        await decide_approval(
            self.world.engine,
            self.approver,
            approval_id,
            "approve",
            jobs=decision,
            on_reject=REJECTS,
        )
        [execute] = decision.jobs
        await self.world.executor(execute)
        await self.world.executor(execute)
        [job] = self.releases.jobs
        self.releases.jobs.clear()
        assert job.type == MILESTONE_RELEASE
        handler = MilestoneReleaseHandler(
            self.world.engine,
            self.providers.bundle(),
            None,
            poll_budget=0,
            attester=self.providers.chain.attester,
        )
        assert (
            await handler.release(self.world.people.tenant, self.payment_id, args.milestone_index)
            == "released"
        )
        await handler(job)  # redelivered: nothing more
        self.approved.append((args.milestone_index, args))

    async def summary(self) -> PurchaseOrderPaymentSummary:
        order = await award_service.get_purchase_order(self.world.engine, self.owner, self.po.id)
        assert order.payment is not None
        return order.payment

    async def progress(self) -> tuple[str, int, str | None]:
        shown = await self.summary()
        return shown.state, shown.released_bps, shown.next_milestone

    async def statuses(self) -> tuple[list[str], list[str]]:
        """Each milestone's status, as the buyer and as the factory see it."""
        mine = await milestones.list_milestones(
            self.world.engine, self.owner, self.po.id, settings=SETTINGS
        )
        theirs = await milestones.factory_order(self.world.engine, self.access, settings=SETTINGS)
        return [m.status for m in mine], [m.status for m in theirs.milestones]


async def pay_on_the_tooling_plan(world: World, providers: FakeProviders) -> Tooling:
    """Requested with the default milestones, approved by the buyer, charged and funded."""
    po = await sent_po(world)
    owner = buyer(world.people)
    account = await service.create_payout_account(
        world.engine, owner, po.supplier_id, ACCOUNT, providers.bank_details
    )
    card = await service.enroll_payment_method(
        world.engine, owner, card_token("e2e-tooling"), providers.card, consumer_country="US"
    )
    requested = await service.request_po_payment(
        world.engine,
        owner,
        po.id,
        PayPurchaseOrderRequest(payment_method_id=card.id, payout_account_id=account.id),
        providers.bundle(),
        settings=SETTINGS,
        today=RATE_DAY,
    )
    assert [(m.key, m.name, m.bps) for m in requested.payment.milestones] == PLAN

    wakes = FakeJobQueue()
    register_payment_act_tools(
        world.registry, PaymentActDeps(db=world.engine, providers=providers.bundle(), jobs=wakes)
    )
    decision = FakeJobQueue()
    await decide_approval(
        world.engine, owner, requested.approval_id, "approve", jobs=decision, on_reject=REJECTS
    )
    [execute] = decision.jobs
    await world.executor(execute)
    pipeline = PaymentAdvanceHandler(world.engine, providers.bundle(), wakes, poll_budget=0)
    steps = 0
    while wakes.jobs:
        envelope = wakes.jobs.pop(0)
        assert envelope.type == PAYMENT_ADVANCE
        await pipeline(envelope)
        steps += 1
        assert steps <= 6, "the pipeline keeps waking itself"
    payment = await service.get_po_payment(world.engine, owner, po.id, SETTINGS)
    assert payment.state == "funded"

    link = await issue_factory_link(world.engine, owner, po.id, settings=SETTINGS)
    assert link.url == f"{SETTINGS.factory_page_url}#{link.token}"
    access = await resolve_factory_link(world.engine, link.token)
    assert access.payment_id == payment.id
    tooling = Tooling(world, providers, po, payment.id, access)
    register_milestone_act_tools(
        world.registry,
        MilestoneActDeps(db=world.engine, providers=providers.bundle(), jobs=tooling.releases),
    )
    return tooling


type Step = tuple[str, int | None]  # (audit action, milestone index)


async def milestone_actions(tooling: Tooling) -> list[set[Step]]:
    """The milestone story in the audit log, one set per transaction (its events share
    `created_at`, their order inside it is not kept), oldest first; documents added or
    removed left out."""
    async with tenant_session(tooling.world.engine, tooling.world.people.tenant) as session:
        events: Sequence[AuditEvent] = list(
            await session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.action.like("purchase_order.%"),
                    AuditEvent.payload["payment_id"].astext == str(tooling.payment_id),
                )
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
        )
    for event in events:
        # Ids, indexes and codes only: never a name, a note or a reason.
        assert "filename" not in event.payload and "note" not in event.payload
        assert REASON not in str(event.payload)
    story: list[set[Step]] = []
    at: datetime | None = None
    for event in events:
        if "document" in event.action:
            continue
        if event.created_at != at:
            story.append(set())
            at = event.created_at
        story[-1].add((event.action, event.payload.get("milestone_index")))
    return story


async def test_a_tooling_order_is_paid_milestone_by_milestone(
    world: World, providers: FakeProviders, usd_rate: object
) -> None:
    tooling = await pay_on_the_tooling_plan(world, providers)
    chain = providers.chain
    assert await tooling.progress() == ("funded", 0, "Tool start")
    assert await tooling.statuses() == (
        ["awaiting_documents", "locked", "locked"],
        ["awaiting_documents", "locked", "locked"],
    )

    # The factory confirms its order, then sends the tool's steel photos and certificate.
    accepted = await accept_order(world.engine, tooling.access)
    assert (await accept_order(world.engine, tooling.access)).accepted_at == accepted.accepted_at
    await tooling.upload_required(0)
    tool_start = await tooling.submit(0, "Steel arrived, machining started")
    await tooling.approve_and_release(tool_start)
    assert await tooling.progress() == ("funded", 3_000, "T1")
    assert await tooling.statuses() == (
        ["released", "awaiting_documents", "locked"],
        ["released", "awaiting_documents", "locked"],
    )

    # T1 is sent back once: the buyer asks for changes, with a reason code.
    sent = await tooling.upload_required(1)
    first_t1 = await tooling.submit(1, "T1 samples shipped")
    await tooling.queued(first_t1)
    await milestones.request_milestone_changes(
        world.engine,
        tooling.owner,
        tooling.po.id,
        1,
        REASON,
        reason_code="dimensional_nonconformance",
        jobs=FakeJobQueue(),
        settings=SETTINGS,
    )
    async with tenant_session(world.engine, world.people.tenant) as session:
        refused = await session.get(Approval, first_t1)
        assert refused is not None and refused.status == "rejected"
    assert tooling.releases.jobs == []
    assert await tooling.statuses() == (
        ["released", "changes_requested", "locked"],
        ["released", "changes_requested", "locked"],
    )
    order = await milestones.factory_order(world.engine, tooling.access, settings=SETTINGS)
    t1 = order.milestones[1]
    assert (t1.rejection_code, t1.rejection_reason) == ("dimensional_nonconformance", REASON)
    # The factory replaces the refused report only: the photos it sent stay.
    corrected = await tooling.upload(1, "t1_report", version=2)
    order = await milestones.factory_order(world.engine, tooling.access, settings=SETTINGS)
    [report] = [r for r in order.milestones[1].requirements if r.doc_type == "t1_report"]
    [old] = [d for d in report.uploaded if d.filename.startswith("t1_report-v1")]
    await remove_document(world.engine, tooling.access, tooling.payment_id, 1, old.id)
    second_t1 = await tooling.submit(1, "Report corrected after the T1 review")
    resent = await tooling.queued(second_t1)
    assert resent.attempt == 2
    assert {d.sha256 for d in resent.documents} == {corrected, sent[1]}
    assert sent[0] not in {d.sha256 for d in resent.documents}
    await tooling.approve_and_release(second_t1)
    assert await tooling.progress() == ("funded", 6_000, "PPAP")

    # PPAP: the last 40%, and the payment is complete.
    await tooling.upload_required(2)
    ppap = await tooling.submit(2, "PSW signed, production run done")
    await tooling.approve_and_release(ppap)
    assert await tooling.progress() == ("funded", 10_000, None)
    assert await tooling.statuses() == (
        ["released", "released", "released"],
        ["released", "released", "released"],
    )

    # Complete: still `funded` for the API, with `completed_at`; the payment's own steps.
    payment = await service.get_po_payment(world.engine, tooling.owner, tooling.po.id, SETTINGS)
    assert (payment.state, payment.needs_attention) == ("funded", False)
    assert payment.completed_at is not None
    assert [(t.step, t.state) for t in payment.chain.transactions] == [
        ("mint", "confirmed"),
        ("create_escrow", "confirmed"),
        ("fund", "confirmed"),
    ]
    # Three release transactions, each confirmed, one per milestone.
    rows = await chain_transactions(world.engine, world.people.tenant, payment.id)
    releases = [(t.milestone_index, t.state) for t in rows if t.step == "release"]
    assert releases == [(0, "confirmed"), (1, "confirmed"), (2, "confirmed")]
    # On chain: each gate attested once by Bido's attester, for this PO, with the evidence
    # the person approved; the whole amount paid out to the fixed payout, once.
    escrow_address = payment.chain.escrow_address
    assert escrow_address is not None
    escrow = chain.escrows[escrow_address]
    total = int(payment.charge_amount * 10**SETTINGS.solana_stablecoin_decimals)
    assert (escrow.status, escrow.released, escrow.total) == ("completed", total, total)
    assert [index for index, _ in tooling.approved] == [0, 1, 2]
    for index, args in tooling.approved:
        attestation = chain.attestations[(escrow_address, index)]
        assert (attestation.signer, attestation.gate) == (chain.attester, index)
        assert attestation.po_hash == escrow.po_hash == args.content_hash
        assert attestation.evidence_hash == args.evidence_hash
        released = escrow.milestones[index]
        assert (released.status, released.evidence_hash) == ("released", args.evidence_hash)
        assert released.amount == int(args.token_amount)
    assert sorted(gate for _, gate in chain.attestations) == [0, 1, 2]
    assert chain.balances[chain.payout] == total
    assert chain.balances[escrow.vault] == 0
    assert chain.minted_total == total

    # The record: every step once, in order, ids and codes only.
    assert await milestone_actions(tooling) == [
        {("purchase_order.payment_requested", None)},
        {("purchase_order.payment_charged", None)},
        {("purchase_order.payment_minted", None)},
        {("purchase_order.escrow_created", None)},
        {("purchase_order.escrow_funded", None)},
        {("purchase_order.factory_link_issued", None)},
        {("purchase_order.factory_order_accepted", None)},
        {("purchase_order.milestone_submitted", 0)},
        {("purchase_order.milestone_releasing", 0)},
        {("purchase_order.milestone_released", 0)},
        {("purchase_order.milestone_submitted", 1)},
        {("purchase_order.milestone_changes_requested", 1)},
        {("purchase_order.milestone_submitted", 1)},
        {("purchase_order.milestone_releasing", 1)},
        {("purchase_order.milestone_released", 1)},
        {("purchase_order.milestone_submitted", 2)},
        {("purchase_order.milestone_releasing", 2)},
        # The last release completes the payment, in the same transaction.
        {("purchase_order.milestone_released", 2), ("purchase_order.payment_completed", None)},
    ]
    feed = await rfq_activity(world.engine, tooling.owner, tooling.po.rfq_id, limit=200)
    summaries = {item.summary for item in feed}
    ref = tooling.po.ref
    assert {
        f"Milestone Tool start of purchase order {ref} released from the escrow (30% of the total)",
        f"Milestone T1 of purchase order {ref} released from the escrow (30% of the total)",
        f"Milestone PPAP of purchase order {ref} released from the escrow (40% of the total)",
        f"Changes requested on milestone T1 of purchase order {ref} (dimensional "
        "nonconformance): the factory sends it again",
        f"Payment of purchase order {ref} completed: the escrow released every milestone",
    } <= summaries
    assert not [s for s in summaries if REASON in s or "Steel arrived" in s]
