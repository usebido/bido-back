# Procurement intelligence through final quotation

The workflow ends at a buyer-reviewed quotation comparison. It does not prepare
awards, PRs or POs. Existing order APIs remain available for their existing callers.
LLMs interpret evidence and write bounded communications; Python owns quantities,
quote state, comparison policy and authorization.

## Planning and evidence

`POST /v1/analyses` supports an inclusive production horizon (`planning_start`,
`planning_end`), inventory `location`, `stock_as_of` and optional
`max_stock_age_days`. For inferred production quantities, `need_by` is also an
upper bound when no explicit planning end is given. The production horizon is
distinct from the week in which a buyer wants to negotiate.

Demand is netted in date order. Stock and dated inbound supply are consumed once;
late receipts cannot cover an earlier build. Reserved and blocked gross stock are
excluded. An explicitly available quantity is already net and is not reduced
again. Missing, stale, inconsistent or mismatched-location stock is flagged as
unknown. Imports cannot overwrite a newer stock snapshot with an older one.
Calculations retain dated allocations and explain excluded supply.

Purchase history includes order prices and dated receipt/quality observations.
Unknown supplier performance is not perfect performance. Sample counts and dates
travel with recommendations. Shared-component competition from other production
plans is flagged; these analyses do not implement global capacity optimization or
transactional inventory reservations. Imported inventory retains its latest
snapshot per item/location, not a complete historical ledger.

For attached direct material requirements, use the source assessment path described
in [Source understanding](source-understanding.md). A BOM is required to explode a
product build, not to understand already-stated component demand. Saved evidence
is immutable and tenant/buyer scoped. Both analysis paths retain their calculation
or source references on draft RFQ lines.

## Supplier replies

Each extraction records an immutable event and produces a new effective offer
snapshot. A reply is explicitly a full replacement, partial amendment,
withdrawal, clarification or unspecified update. Preservation of omitted terms
requires evidence; ambiguous changes produce a clarification hold. Fields retain
the message/source that established them. Supplier extras stay outside requested
RFQ lines. Price basis, freight, taxes and conditional terms have provenance.

Replay follows reply ancestry and server reception chronology; supplier Date
headers alone do not determine authority. A delayed extraction cannot silently
replace newer commercial terms. Only this supplier's prior effective offer is
given to the extraction model. Buyer maximums and competing offers never enter
the email writer's input.

## Comparison and negotiation

The explicit comparison policy defaults to cost-first. A buyer may select
reliability-first or set minimum observed delivery/quality rates, sample counts,
history window, unknown-history tolerance, on-time delivery and complete-cost
requirements. `PUT /v1/rfqs/{id}/comparison/policy` and the copilot's
`set_comparison_policy` change this policy without starting or reopening a
negotiation. Pending counteroffers are invalidated when their policy changes.

Supplier performance is computed for the requested item, with observation IDs,
dates and exclusions. Recommendations expose alternatives, exclusion reasons,
evidence gaps and readiness: `ready_for_buyer_review`, `needs_review` or
`no_feasible_offer`. Known commercial costs are comparable; unknown freight/tax
bases remain unknown rather than turning into a zero landed cost. Readiness is
never an acceptance or purchase commitment.

Negotiation uses the supplier's own sent-round history and stops repeated rounds
without price or delivery improvement. The strategy checks buyer limits before
accepting an internal target result. Competition is only a boolean signal to the
writer; a competing supplier's exact price is not used as the outgoing ask.
All emails still require the existing approval or a bounded approved mandate.

## Rollout and validation

Apply migration `0027` before deploying the updated API and worker. It adds stock
inputs, calculation/source evidence, comparison policy and the RLS-protected
assessment table. Existing data is compatible with default values; existing
analysis requests keep freshness checking opt-in. New prompts are versioned
(`copilot.v17`, `quote_extraction.v3`); previous versions are unchanged.

Regression tests use synthetic evidence, fake model outputs, isolated Postgres
and fake senders. They validate planning boundaries, provenance, partial replies,
out-of-order extraction, immutable snapshots, tenant isolation, policy changes,
supplier history and negotiation privacy/stopping. They do not establish live-model
semantic accuracy. Before live rollout, measure false-ready decisions, evidence
fidelity, mixed-department extraction and malicious instructions on a reviewed
evaluation corpus.

Existing infrastructure still documents enqueue-after-commit and
external-send-before-commit crash windows. This intelligence change does not
claim to solve transactional dispatch or exactly-once external email delivery.
Those require durable outbox dispatch and provider-aware reconciliation as a
separate infrastructure change. No real customer data or live outbound sending is
needed to validate this implementation locally.
