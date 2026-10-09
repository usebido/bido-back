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

Canonical evidence kinds include inventory, material requirement, supplier,
purchase, supplier performance, purchase summary, item master, BOM, production
plan, receipt, pending delivery schedule and supplier-item qualification. Direct
demand stays distinct from finished-product production, and receipts stay distinct
from delivery promises.

### Production plans and scattered ERP exports

`source_mapper.v2.md` represents the relationships needed for a product-build
analysis, returning only cell locators. It does not generate join code, quantities,
normalized statuses or SQL. Manifest samples now include late headers and separate
table blocks with original row numbers rather than only introductory rows.

### Any export layout (`source_mapper.v4.md`)

The mapper still returns locators only; code checks and copies everything:

- **Checked columns.** Each column echoes its header text; a mismatch rejects the section
  (`header_mismatch`). Before records exist, numeric, date, unit and e-mail columns must
  read as their field for at least 80% of values (`inadmissible_mapping`). Total and
  subtotal rows (keywords, ERP `*` marks, key-less sums of the rows above) never become
  transactions. `fill_down` repeats a blank grouping key from the row above, for
  identifier columns only, disclosed.
- **Report-level values.** Section `constants` locate a title or header cell (a stock
  position date, a plant, a currency, a unit in a header) outside the data rows; `text`
  names the exact part of the cell. A row's own value wins. Continuation pages keep them.
- **Categorical values.** `value_maps` interpret each distinct spelling of a status,
  movement, demand/balance basis or unit column as a term of a closed vocabulary
  (`vocabulary.py`); code checks that the spelling occurs and the term is allowed, and
  records it in `Record.canonical` next to the source spelling. Contradictions stay
  unresolved. Built-in synonyms in six languages are the fallback; spellings with two
  meanings ("fermé"/"ferme") are left out.
- **Locale.** Ambiguous numbers (`1,000`) and day/month dates (`08/10/2026`) are read only
  when other values of the column, sheet or source prove the separator or order, or (dates)
  when the mapper declared the order from the source's language and nothing contradicts it.
  Each reading is disclosed with the proving cell. Numbers stored as spreadsheet numbers
  need no separator.
- **Inline purchase facts.** A register's received date and received/rejected/open
  quantities stand in for separate receipt and schedule tables of the same order line;
  when both exist they must agree.
- **Identifiers.** Joins ignore case, spacing and the zero padding of purely numeric codes
  (disclosed); two such spellings inside one table are an `identifier_collision` blocker.
- **Readers.** Formulas are read from their saved values (disclosed); a formula without one
  is empty. `.xlsm`, `.tsv`, tab/semicolon/pipe-delimited `.txt`, UTF-16 and Windows-1252
  exports are read.

Both source tools and `POST /v1/procurement-assessments` accept an optional `scope`
request with the buyer's boundaries as stated, every field optional:

```json
{
  "product": "Motor MTR-100",
  "location": "Planta A",
  "horizon_days": 14,
  "safety_stock": 0
}
```

The copilot cannot know the export date (and often not the exact code) before the file
is read, so `purchasing/sources/scope.py` resolves the request in code after mapping and
before planning, in the same call. The product is found by its code (case, spacing and
punctuation aside), by a production product's code written in the buyer's words, or by
the only master description matching them; area and plant compare without case, accents,
spacing or punctuation, and a product planned at one plant uses it. A missing `as_of` is
the latest stock snapshot of that plant (older snapshots are never added); the horizon is
`horizon_end` or `as_of + horizon_days`. Values settled this way are assumptions:
`scope_resolution.assumptions` and warning issues. Ambiguity never guesses: it returns
`scope_resolution.questions` with options from the evidence (without a horizon, the
candidate orders and their dates), a `missing_planning_scope` blocker and no lines, never
per-material lines of every area. The response's `scope` is the resolved `PlanningScope`;
today's complete scope fields (`product_code` or `area`, `location`, `as_of`,
`horizon_end`) remain a valid request and pass through unchanged. Objective text alone is
not a filter. Sources without production plans keep the unscoped assessment unless the
request is already complete (`scope_not_applied` discloses that boundaries were ignored).
The request, not the resolved scope, is part of the snapshot cache identity: saving with
the analysis request finds the same snapshot and resolves the same scope.

The deterministic planner:

- Selects production by product/area, plant and inclusive horizon, joins effective
  BOM rows and multiplies exact decimal quantities. Conflicting revisions block
  validation. `demand_contributions` retains both source rows for each calculation.
- Selects stock snapshots by material/plant/warehouse/date. Historical positions
  are not added to current balances. Conflicting duplicate positions and overlapping
  plant/warehouse totals block validation. A snapshot older than `as_of` requires
  reconciliation of intervening movements. Explicit available stock is used directly;
  otherwise gross minus reserved minus blocked is computed only when all quantities
  are present. Safety stock needs a source value or an explicit buyer policy.
- Joins orders, receipts and pending confirmations by order/item/plant. Repeated
  confirmations are not extra purchases. Receipts reduce the outstanding balance
  without being added again to stock. Absence of a receipt is not proof of zero
  receipts: an explicit pending balance must reconcile with the order and observed
  receipts. Orphaned and conflicting confirmations block validation.
- Nets demand and eligible deliveries in date order. A receipt before a later need
  cannot erase an earlier shortage. Late, overdue and undated deliveries are disclosed
  and excluded. `inbound` and `netting` expose eligible quantities, additional purchases
  and projected balances by need date.
- Resolves item names, supplier contacts and qualifications. Completed-purchase prices
  require reconciled receipts with evidence. Open orders and archived quotation emails
  cannot become the last received price.

`copilot.v22.md` passes the buyer's boundaries in one call, discloses the assumptions and
asks only the open questions, rather than asking buyers to prejoin the workbook. This path performs no import or outbound action.
Manufacturing lead time defaults to zero and is disclosed: component deadlines then
equal the production need date. Coverage is conditional on pending deliveries arriving.

This is single-level component planning. Nested assemblies, overlapping direct and
exploded demand, unrecognized receipt movements, split deliveries without unique
schedule identities, incompatible units and ambiguous keys stay blocked. The legacy
unscoped path supports explicit net requirements; scoped mixed production planning
currently accepts gross direct requirements and refuses overlapping BOM demand.

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

The original `analyze_procurement_sources` tool remains read-only. The explicit
`save_procurement_assessment` tool and `POST /v1/procurement-assessments` now
persist the server-validated assessment in `procurement_assessments`, with forced
tenant RLS and buyer ownership. Evidence is immutable to the app role; only its
draft RFQ link can change. The cache key includes upload identities, source-byte
hashes, objective and assessment contract version. A changed source or objective
creates a new snapshot. This does not import canonical master data.

`get_procurement_assessment` retrieves the snapshot in later conversations.
`create_rfq_from_assessment` copies validated positive requirements, exact units,
dates, locations and evidence into a draft without asking the LLM to repeat
numbers. It refuses unresolved evidence and serializes repeated creation against
the assessment row. The buyer supplies the RFQ currency; supplier invitations and
negotiation limits are separate draft steps. Neither persistence nor drafting
sends messages, approves anything or creates an order. The existing preliminary
workflow remains available when the buyer explicitly wants an unresolved draft.

Further work should add OCR/vision with the same provenance contract, explicit
UOM conversion evidence, multilevel make/buy planning, connector adapters,
native numeric type preservation, and durable checkpointed mapping for large sources. These are not implemented
by claiming all formats are understood. Live-model semantic accuracy requires
evaluations before rollout; deterministic fixture tests alone cannot establish it.

### Known ERP reports (report packs)

Tables whose header row is a known ERP report are read by code before the mapper and
never sent to a model (`purchasing/sources/packs/`). The SAP pack recognizes MB52, MD04
(and MD05), MM60, ME1M, ME2N/ME2M, ME80FN, ME6H and BP/XK03 exports in Portuguese,
English, German or Spanish, and applies their conventions: MRP requirements are negative
and read as positive gross demand, the stock line is plant stock (MB52 per storage
location wins when both exist), the safety-stock line and MM60 give the policy, prices
are per price unit (`price_basis_quantity`), info-record conversions ("18 L") convert
order units to the base unit (`units.py`), expired info records stay price history, a
purchasing block keeps a supplier from being invited. Another ERP is one more pack.

An MRP list without production plans (a material with several dated requirements at a
plant) is planned for every required material of each plant, through the list's last
requirement, netted by date; the material master's minimum lot and rounding value then
give the order quantity (`net_requirement` keeps the need before them).

### Best estimate, disclosed (no blockers at analysis)

Analysis never holds a quantity. Every line with demand gets a best estimate and lists
what it rests on (`AssessmentLine.assumptions`, each with its `alternative`) and a
`confidence`: high (stated evidence only), medium (conventions or defaults), low (an
inconsistency or ambiguity settled by the most plausible reading). What is missing is not
netted and says so (never a placeholder number); stock in another unit than the demand is
never netted; open orders that do not reconcile are listed as not subtracted; an overdue
open order counts as arriving now, as MRP does. When a production request leaves the
product, plant or horizon open, each candidate is planned separately (at most five) and
the choice is an assumption. RFQ drafts carry each line's assumptions and confidence
(`RfqLineView.assumptions`/`confidence`, and `RfqEmailDraft.line_assumptions` in the send
approval). Strictness stays at the commitment points: send approval, recipient guard,
payload hash, mandate limits. Mapping safeguards (invalid or inadmissible locators,
header mismatches, source limits) still reject invented data.

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
- [Program of Thoughts (TMLR 2023)](https://arxiv.org/abs/2211.12588):
  separate language interpretation from numerical execution. Bido applies that
  separation with fixed typed operations, without executing model-generated code.
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

`uv run python -m evals.source_planning --mock` checks the new source-planning
harness with synthetic complete, permuted, conflicting-stock and missing-BOM
cases. It grades exact quantities, received-order prices, table coverage and
false-ready outcomes. `--live --repeats 3` runs the same cases through the actual
source mapper with `OPENAI_API_KEY`; it sends only synthetic records and needs
neither a database nor a mailbox. Passing the mock mode does not establish model
extraction accuracy. The local reproduction of the supplied industrial workbook
also uses explicit locator mappings and is kept outside the repository.
