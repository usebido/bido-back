"""Job type names shared by producers and the worker registry."""

# One approved milestone of a funded escrow released on chain, from
# `bido_back.payments.milestones.proposals`.
# payload: {"payment_id": "<uuid>", "milestone_index": <int>}
from bido_back.payments.milestones.proposals import MILESTONE_RELEASE as MILESTONE_RELEASE

# One chain step of a charged payment (mint, create the escrow, fund it), from
# `bido_back.payments.proposals`. payload: {"payment_id": "<uuid>"}
from bido_back.payments.proposals import PAYMENT_ADVANCE as PAYMENT_ADVANCE

APPROVAL_EXECUTE = "approval.execute"
QUOTE_EXTRACT = "quote.extract"  # payload: {"message_id": "<uuid>"}
# A supplier's reply to a sent purchase order. payload: {"message_id": "<uuid>"}
PO_RESPONSE = "po.response"
FX_REFRESH = "fx.refresh"  # payload: {}
# payload: {}. tenant_id = the all-zeros UUID for the scheduled every-tenant run
# (EventBridge Scheduler), or a real tenant id to sync just that tenant.
MAILBOX_SYNC = "mailbox.sync"
TEMPLATE_SCAN = "template.scan"  # payload: {"scan_id": "<uuid>"}
TEMPLATE_BUILD = "template.build"  # payload: {"template_id": "<uuid>"}
FOLLOWUPS_SWEEP = (
    "followups.sweep"  # payload: {}. tenant_id = all-zeros for every tenant (scheduled)
)
NEGOTIATION_SWEEP = (
    "negotiation.sweep"  # payload: {}. tenant_id = all-zeros for every tenant (scheduled)
)
# payload: {}. Reads one company's files not yet read, for content search.
FILES_INDEX = "files.index"
# payload: {"rfq_id": "<uuid>"}. One RFQ's negotiation planned again right after a
# supplier reply (`bido_back.negotiation.jobs`); the sweep stays the safety net.
NEGOTIATION_REPLAN = "negotiation.replan"
