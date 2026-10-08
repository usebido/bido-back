# Source understanding for procurement

## Problem and architecture review

The procurement copilot currently funnels uploaded workbooks into six import
kinds and then into a BOM-based product-build analysis. That is too narrow for
material requisitions, replenishment or MRP exports that already identify the
components and dates. A BOM is one way of deriving demand, not a prerequisite
for understanding existing demand.

The failure has several independent causes:

- `copilot.v15.md` explicitly asks for a product, quantity and BOM whenever an
  import contains no BOM. `run_purchase_analysis` likewise resolves a product
  and loads its BOM before calculating requirements.
- `purchasing/importers.py` and the sheet mapper's output contract cannot
  represent direct material requirements or supplier performance. A more
  capable model cannot recover semantics that the schema excludes.
- The legacy mapper sees previews of unresolved sheets. Rule-recognized source
  tables may be absent from its context, and a preview is not full inspection.
- Supplier identifiers require a cross-source join. Current rules can resolve
  the supplied test export, but conflicting master data previously allowed a
  first-name-wins interpretation, and unmapped master tables were unavailable.
- Consolidated reports repeat underlying quantities. Treating a report as an
  additional inventory or demand source risks duplicate or overwritten facts.
- `documents/extract.py` already supports located text from several formats;
  `quotes/validation.py` already demonstrates deterministic evidence validation.
  Purchasing had not adopted this separation between evidence and interpretation.
- Tracked `workbook_planner`, `workbook_analyst` and `workbook_reviewer` prompts
  have no executing workflow in the checked-out source. Prompt files alone do
  not implement staged understanding. The new workflow below has its own
  versioned prompt and executable contracts.

The surrounding architecture remains appropriate: FastAPI and a worker share
Postgres; tenant transactions and forced RLS isolate business data; Agno and
OpenAI stay behind `agno_adapter`; typed tools expose business operations;
Bido owns approvals and guarded sending. The linked engineering infrastructure
proposal is older than the running code and still describes an earlier LLM
stack, so current repository invariants take precedence.

Separate follow-ups found during review: the worker documents an
`enqueue-after-commit` loss window, and the approval executor documents an
external-effect-before-commit retry window. An invariant requiring an outbox
is not evidence that a durable outbox is already implemented. Closing those
windows needs its own focused change, beyond source understanding.

## Implemented workflow

```mermaid
flowchart LR
    A[Files or connector evidence] --> B[Located tables and text]
    B --> C[Manifest and bounded pages]
    C --> D[Parallel semantic mapping]
    D --> E[Validate locators and copy source values]
    E --> F[Resolve identities and reconcile quantities]
    F --> G[Assessment with evidence and open issues]
    G --> H[Existing draft and approval workflows]
```

`purchasing/sources` supplies a read-only path through these stages:

1. Adapters preserve source IDs, table names and coordinates. XLSX/CSV use table
   evidence; PDF text, DOCX and text use the existing extraction adapter and
   located text slices. `EvidenceTable` is independent of file format and can
   also receive connector records. Scans/images require a future vision
   adapter; unsupported files, limits and partial coverage are explicit issues.
2. Code inventories all sources, then presents all rows in bounded pages.
   Up to four interpretation calls run concurrently; pages of the same table
   run in sequence to retain header interpretation. Source/page/cell/character
   limits fail explicitly rather than claiming complete coverage.
3. The tool-free mapper proposes semantic table columns or exact text slices.
   It cannot manufacture values or invoke an importer. Arbitrary header names
   and languages are interpreted against a domain catalog, rather than requiring
   a new header alias for every customer.
4. Code checks field names, row and column bounds, slices and overlaps, copies
   original values, and records field-level provenance. Invalid proposals and
   failed pages remain visible. Typed output constrains structure; it does not
   prove the model interpreted the business meaning correctly.
5. Deterministic assessment resolves supplier codes/names, retains purchase
   history and supplier performance, reconciles source calculations, and reports
   direct material demand alongside available/inbound/safety stock. Consolidated
   purchase summaries are a separate record kind and never add to demand/stock.
6. The copilot presents reported quantities separately from validated
   calculations and asks about specific unresolved semantics. It no longer asks
   for a BOM simply because a direct-demand source was not imported.

The existing importer also resolves explicit supplier identity pairs across all
source sheets, refuses ambiguous/unresolved codes even on confirmed imports,
preserves supplied item metadata, and rejects excess sheets instead of silently
ignoring them.

## Business semantics and limits

Canonical evidence kinds are inventory, material requirement, supplier,
purchase, supplier performance and purchase summary. They complement the
existing persisted items/BOM/history/plan model; they do not pretend a material
requirement is a production plan or supplier OTIF is a purchase receipt date.

For explicit gross demand, with compatible units and confirmed timely receipts:

`to_buy = max(0, demand + absolute_safety_stock - available - eligible_on_order)`

Explicit already-netted demand is used directly. Available stock is not reduced
again by blocked or reserved quantities. A price UOM is not a physical inventory
conversion. Historical purchases do not imply the currency of an inventory
valuation. Supplier metrics retain their source dimensions; no ranking weights
are invented. Missing numbers are not zero. Multiple planning records with an
unreconciled scope are held rather than summed or netted independently.

The supplied test export has useful reported purchase needs, but physical units,
receipt timing and gross/net demand semantics need review before quantities can
be called validated. Source-reported calculations remain available during that
review. The assessment also detects an inventory inconsistency: one row reports
6,100 available plus 2,200 blocked against 8,200 total. It does not assume
that other residual stock gaps must equal reservations. This is different from
claiming the file lacks demand.

This assessment does not persist normalized requirements/evaluations, mint an
analysis ID, send messages, create orders or approve anything. For now a buyer
can use the existing draft RFQ flow; unresolved evidence must retain its existing
preliminary-validation hold. Source analysis is tied to authorized attachments
in the conversation; persisted company imports keep their existing behavior.
A future write path needs versioned canonical facts, source identity/revisions,
RLS migrations and idempotent application after review. It must not turn this
read-only interpretation tool into an action executor.

Further work should add OCR/vision with the same provenance contract, explicit
UOM conversion evidence, time-phased multi-demand netting, connector adapters,
native numeric type preservation, and durable checkpointed mapping for large sources. These are not implemented
by claiming all formats are understood. Live-model semantic accuracy requires
evaluations before rollout; deterministic fixture tests alone cannot establish it.

## Research informing the design

- [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents):
  code-owned workflows and bounded parallelization suit separable interpretation
  tasks; model calls need observable interfaces and evaluation.
- [SpreadsheetLLM (Microsoft Research)](https://arxiv.org/abs/2407.09025):
  preserve spreadsheet structure and table boundaries instead of treating a
  workbook as a single flat string. Compression cannot substitute for coverage.
- [Docling technical report](https://arxiv.org/abs/2408.09869):
  a shared document representation separates format parsing from downstream
  understanding. This design reuses Bido's adapters without adding Docling.
- [Can Foundation Models Wrangle Your Data?](https://www.vldb.org/pvldb/vol16/p738-narayan.pdf):
  schema matching, entity matching and error detection are distinct evaluation
  tasks; a fluent narrative is not an ingestion correctness metric.
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs):
  schema adherence is useful, but outputs can still contain semantic errors.
  Source-grounded validation remains necessary.
- [OpenAI evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices):
  measure task-specific outcomes, adversarial cases and regressions, including
  tool selection and critical failure rates.
- [SAP: Net Requirements Calculation](https://help.sap.com/saphelp_SCM700_ehp01/helpdata/en/a4/2ec95360267614e10000000a174cb4/content.htm?no_cache=true):
  requirements and supply have dates and coverage relationships; procurement
  analysis is not necessarily a new BOM explosion.
- [Microsoft: Safety stock](https://learn.microsoft.com/en-us/dynamics365/supply-chain/master-planning/safety-stock-replenishment):
  safety-stock replenishment is an explicit planning policy, not an arbitrary
  percentage applied to every source quantity.

## Validation approach

Tests assert canonical structures, exact copied values and provenance, complete
page iteration, bounded concurrency, invalid/overlapping references, source
limits, identifier resolution, unit conflicts, demand basis, receipt dates,
duplicate scope and tenant/user/conversation access. They use synthetic fixtures
and injected structured mapper results; no real customer data is sent to a model.

The local workbook assessment is an additional deterministic reproduction with
explicitly supplied mappings. It proves reconciliation of the supplied evidence,
not the production mapper's accuracy. A live evaluation corpus should vary
languages, headers, table order, late headers, merged/hidden sheets, multiple
tables, inconsistent codes, locale-dependent decimals, gross versus net demand,
units/conversions, duplicated consolidated views, scanned/partial sources and
embedded instructions. Track false-ready rate and evidence fidelity in addition
to extraction recall and end-to-end task completion.
