# Decisions

What the prototype has taught us, and the choices that came out of it. The code shows
*what* Bido does; this file keeps *why*, so neither a person nor an agent undoes a choice
by accident or reruns an experiment we already ran.

The invariants that protect tenants, approvals and side effects live in
[`AGENTS.md`](../AGENTS.md) and are not repeated here. Negotiation design is in
[`negotiation.md`](negotiation.md), large ERP sources in
[`large-sources.md`](large-sources.md).

**How to add an entry:** a few lines, newest first within a section. Give the date, the
decision, why we made it, what would make us change it, and the PR. If a decision is
reversed, edit its entry and link the PR that reversed it. Keep the reversal in the entry
rather than deleting it.

## Settled

### Copilot reply language follows the buyer's latest message (2026-10-12)

- **Decision:** the copilot answers in the language of the buyer's latest message that has
  one.
  - A message with no language of its own keeps the previous language. Examples: "ok",
    a part number, files sent without text, text pasted from a supplier's email.
  - With no buyer text at all, the analysis writer answers in Portuguese.
- **Why:** "keep the language the conversation started in" failed in practice. The
  copilot sees only the last 10 runs, so in a long conversation it inferred the starting
  language from its own replies and kept answering in Portuguese to a buyer writing
  English.
- **Would change if:** buyers on a mixed-language team want one fixed language per
  user. That would call for a setting on the user, not inference.
- **PRs:** [#126](https://github.com/usebido/bido-back/pull/126), which reverses the
  fixed reply language from 9675ebc.

### A negotiation is ready to close once every supplier has given its final offer (2026-10-12)

- **Decision:** the RFQ moves to `ready_to_close` in either of two cases:
  - every line is within target (this rule is still gated by `early_close`);
  - every supplier has given its final offer, whatever `early_close` says.
- **Why:** in the first live negotiation (RFQ-8), a final offer that was within the
  maximum but above the target left the negotiation `open` until the deadline. Nothing
  told the buyer it was time to decide. Once every supplier has given its last word there
  is nothing left to negotiate.
- **Disclosure:** offers above target carry `above_target`. The recommendation is
  computed exactly as closing would compute it.
- **PRs:** [#132](https://github.com/usebido/bido-back/pull/132).

### An award closes the live negotiation (2026-10-12)

- **Decision:** executing an award closes the negotiation in the same transaction.
  Pending negotiation emails are cancelled and their approvals expire.
- **Why:** RFQ-5 showed "Awarded" and "Negotiating" at once, with an active mandate that
  could still send.
- **PRs:** [#131](https://github.com/usebido/bido-back/pull/131).

### Award and purchase order by chat, behind the same approvals (2026-10-10)

- **Decision:** the copilot can prepare an award and send a PO. Each is a two-call
  preview and then a request that goes through the same `award_rfq` and PO approvals
  as the RFQ page.
- **Mandates never cover these steps.**
- **Why:** the copilot used to refuse to award, which pushed the buyer to the Award tab
  mid-conversation. The approval is what protects the buyer, not the surface they use.
- **PRs:** [#110](https://github.com/usebido/bido-back/pull/110).

### Files sent without text are analysed, never imported (2026-10-10)

- **Decision:** a message with files and no text runs an analysis. Importing into the
  company's data needs the buyer to ask for it.
- **Why:** the old standing instruction ("import and analyse") imported 8 SAP exports
  into a tenant on its own and created duplicate suppliers.
- **PRs:** [#109](https://github.com/usebido/bido-back/pull/109).

### The RFQ is the PDF; the email is a cover note (2026-10-09)

- **Decision:** the RFQ email body is only a short cover note in the supplier's
  language. The PDF carries the items, terms and questions.
- **Why:** that is how buyers send RFQs. Before this, the body and the PDF duplicated
  each other and drifted apart.
- **PRs:** [#92](https://github.com/usebido/bido-back/pull/92).

### Supplier documents use the supplier's language; line descriptions are not translated (2026-10)

- **Decision:** the language of the supplier documents is decided once, at send time
  (`rfqs/language.py`). It is the first of these that applies:
  1. the supplier's own language;
  2. its country;
  3. its company suffix ("Srl" means Italian);
  4. the buyer's template;
  5. the buyer;
  6. the email domain;
  7. the standard template.
- **What gets translated:** only the buyer's template text (greeting and labels).
- **What doesn't:** line descriptions stay in the buyer's words. They are the
  contractual spec, and a model rewording them is a model restating a fact.
- **Open:** whether to translate them anyway. See below.

### A free mailbox is confirmed per address, never per domain (2026-10-08)

- **Decision:** confirming a supplier at gmail.com, outlook.com and similar providers
  confirms that exact address only.
- **Why:** confirming `gmail.com` would have let every gmail.com address receive Bido's
  emails without a person looking.
- **PRs:** [#77](https://github.com/usebido/bido-back/pull/77).

### The RFQ currency is resolved, never defaulted (2026-10-08)

- **Decision:** the currency is resolved in this order:
  1. the currency the buyer stated;
  2. the analysis's single currency;
  3. the currency of at least ⅔ of the tenant's imported purchase orders, counted by
     orders rather than spend.
- **If none applies:** the RFQ is not created, and the copilot asks the buyer.
- **Why:** a buyer in Brazil got an RFQ in EUR because EUR was hard-coded.
- **PRs:** [#49](https://github.com/usebido/bido-back/pull/49).
- **Known gap:** an import without a currency column still stores EUR
  (`purchasing/service.py`, `DEFAULT_CURRENCY`).

### No RFQ is sent without a reply-by date (2026-10-08)

- **Decision:** sending is refused when the reply-by date is missing or in the past.
- **Why:** without it, follow-ups and negotiation have nothing to anchor on.
- **PRs:** [#48](https://github.com/usebido/bido-back/pull/48).

### The copilot only claims what a tool result confirms (2026-10-08)

- **Decision:** the copilot says a change was made only when a tool result in the same
  turn confirms it.
- **When no tool can store a request:** it says so, and never squeezes it into another
  field.
- **Why:** it told a tester it had saved payment terms that no field could hold.
- **PRs:** [#47](https://github.com/usebido/bido-back/pull/47). This is the B2B form
  of "the agent chooses the action, never invents the fact".

## Open

Choices we have not made yet. Each states the recommendation and the strongest objection.

### Translate RFQ line descriptions into the document language?

- **Today:** an Italian PDF carries a Portuguese description. The MPN and manufacturer
  are the only parts the supplier can read.
- **Recommendation:** translate at send time.
  - Print the original under the translation.
  - Keep the part number, MPN and manufacturer as they are.
  - Have code check that every number and unit in the original appears in the
    translation, and fall back to the original if one is missing.
- **Objection:** the buyer who approves the send usually cannot read the target
  language, so the approval does not really review the translation. A subtle
  mistranslation of a spec could lead to a dispute with the supplier.

### Fewer confirmations for draft steps the buyer already asked for

- **Today:** creating one RFQ with one invite took three confirmation rounds.
- **Proposal:** run the create and invite draft steps in the same turn when the buyer
  explicitly asked and nothing is missing. Sending still goes through approvals.
- **Objection:** the model judges what counts as "explicitly asked". The worst case is
  an editable draft the buyer never previewed.
- **PRs:** [#121](https://github.com/usebido/bido-back/pull/121), open, as
  `copilot.v36` (main's v35 plus this rule).

### Dates written by the copilot

- **Today:** the copilot writes numeric dates ("07/11/2026"), while the UI shows
  month-first numeric dates in English. The same date reads two ways on one screen.
- **Recommendation:** use month names everywhere ("7 Nov 2026"), with one formatter in
  the front.
- **Objection:** dd/mm is natural for a Brazilian reader, and the prompt's examples
  drive table layouts.

### The UI language as a signal to the copilot

- **Today:** the front sends no locale, so a conversation that starts with files only
  has no language signal.
- **Option:** an optional `locale` field on `POST /v1/copilot/messages`. It is an
  additive contract change.

### Scope: the PRD vs. what is built

- **The 2026-10-06 PRD** scoped the MVP to the email RFQ agent, Europe-first, with
  negotiation, ERP and POs out of scope.
- **What is built:** the code now covers negotiation, award, PO, SAP imports and PO
  payments.
- **Open:** which of these the first pilot actually uses. Decide this with a customer
  rather than in the code.

## What each explored flow taught us

Where each flow stands as a prototype, from live end-to-end tests on dev.

| Flow | State | What we learned |
|---|---|---|
| Purchase analysis from SAP exports | Built for demos; no live ERP | Real exports (MB52, ME2M, CS12, MD04, XK03) are messy. A model reads a bounded view and code computes every row ([`large-sources.md`](large-sources.md)). |
| RFQ by email | Works end to end | Supplier replies typed in the email body are extracted correctly in about 3 minutes. The SAP demo suppliers use `.example` domains, so their mail bounces; a real test needs a mailbox the tester controls. |
| Negotiation | First real round on 2026-10-12 (RFQ-8) | The mandate auto-approved emails within its bounds. Round 1 asked for the target, the BAFO asked for half the gap, and the maximum never leaked. Same-day fixes: #129–#133. Still open: `PUT /negotiation` waits for the sweep; follow-ups can go out after an award; a failed extraction reads "needs your answer". |
| Award and PO | Built; not yet exercised in a live end-to-end test | Award by chat goes through the same approvals as the page (#110). The PO lifecycle covers acknowledged, received per line, closed and cancelled (#93). |
| PO payments | Built on test rails only (Basis Theory test tenant, Solana devnet) | Card, stablecoin, escrow, and release by milestone (#108, #122, #124). Not in the PRD's MVP; it is a bet on how the money flows, which is itself undecided. |

## Questions only a customer can answer

The prototype cannot settle these. Bring them to discovery calls.

- **Should-cost on direct parts:** is this the real wedge? A large OEM's procurement
  team put the money left on the table at 20–30% (discovery call, 2026-10-01).
- **Agent negotiation:** will a buyer let an agent negotiate under a mandate, or only
  draft for approval email by email?
- **Living next to Ariba or SAP:** where does Bido sit next to a suite the company is
  already rolling out?
- **Money flow:** does the buyer want Bido in the payment at all, as a broker fronting
  the cash, or only up to the PO?
- **The supplier side:** what can distributors and suppliers give us, such as prices,
  stock and lead times? No conversation with one is recorded yet.
