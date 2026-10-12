# Supplier negotiation

How Bido runs a negotiation with a buyer's suppliers, from the RFQ email to the final
quotation comparison. Bido never issues a purchase order here: every outcome ends with the
buyer.

## Principles

- **Code decides, the model writes as little as possible.** Every number, date, ask and
  email type is decided by code. Emails are rendered by code from a phrase catalog. The
  writer models only add an optional short note, which is empty by default, carries no
  digits, names or links, and is checked.
- **One open request per supplier.** Nothing new goes out to a supplier while an email to it
  waits for approval, is being written, or waits for the supplier's answer.
- **Ask only what is missing, once.** Each item asked is recorded. Answers are applied as
  amendments. An item is never asked twice automatically; a second failure goes to the buyer.
- **Never commit.** Emails are requests for a revised offer, a delivery date or a best and
  final offer. They carry a non-binding line and never accept or order anything. Awards and
  purchase orders are outside negotiation mandates.
- **The buyer always sees the state.** For each supplier: waiting for the supplier until a
  date, held for approval, accepted internally with nothing to ask, needs your decision (with
  the reason), needs your answer (a supplier question), or closed.

## Stages

`negotiation/planner.py` decides exactly one next action per supplier from:
- its current offer, with delivery dates computed from the stated lead time and the
  supplier's holiday calendar (`quotes/lead_time.py`);
- the clarification ledger (`followups/ledger.py`);
- the buyer's limits and deadlines;
- every negotiation email decided or sent to that supplier.

| Action | When | Email |
|---|---|---|
| wait | an email is in flight, or the supplier still has time to answer | none |
| clarify | the offer cannot be evaluated: missing price, unclear delivery date, … | clarification (items asked once) |
| revised offer | a price is above target, and the supplier moved or this is its first offer | "request for a revised offer": the target first, then concessions within the maximum, only to suppliers who moved |
| delivery date | the stated lead time misses the need-by date | "can you deliver by <date>? If not, the earliest date" |
| best and final | last allowed round, another supplier already in the final round, or the deadline is near | one per supplier, one common cut-off for the RFQ |
| reminder | our revised-offer or delivery-date request is unanswered by its reply-by date | the same request, once |
| accept internally | price within target, delivery meets the need-by, validity known, no open question | none; the buyer sees why |
| hold | supplier question, review, or out of bounds | none; the buyer decides or answers |
| close | after the best and final, a declared final position, round cap or cut-off | none; open problems are named for the buyer |

When one supplier answer leaves open items *and* a price to negotiate, a single email asks
the open items first and the revised offer after them, with at most three asks.

A declared final position stays final ("best and final", "we cannot go any lower"). Later
replies only update the standing offer.

## When the plan moves

- A supplier reply plans its RFQ at once: quote extraction marks the RFQ and enqueues one
  `negotiation.replan` job per burst of replies. The 15-minute `negotiation.sweep` stays the
  safety net.
- An approved mandate plans its RFQ at once when the RFQ is already out: offers that arrived
  while the negotiation was not started are decided (and, in bounds, sent) right away.
- New limits under a live mandate: propose a new mandate (`POST .../negotiation/mandate`, or
  the copilot's `propose_negotiation_mandate`). It is a replacement: the current mandate keeps
  running until the replacement is approved, which ends it, saves the new limits and
  withdraws the emails decided under the old ones so they are decided again. A rejected
  replacement changes nothing.
- An offer held for the buyer is negotiated as it is with `POST
  .../suppliers/{invite_id}/release-hold`, or the copilot's `release_negotiation_hold` (a
  preview first, then the buyer's confirmation).

## Emails

`supplier_email/` holds one layout for every negotiation email, in pt-BR, en, de, fr, it and
es (the de/fr/it/es catalogs need native review). The blocks are:

1. Salutation.
2. Purpose line.
3. Context line naming the message answered.
4. Positions block (only the positions concerned).
5. Numbered asks, at most three.
6. Reply-by date.
7. Reply instructions.
8. An optional note.
9. A non-binding legal line.
10. A single signature.

Plain text and HTML carry the same numbers and dates. The subject stays fixed in the thread
(`Re: [RFQ-n] …`), and replies thread to the latest message. The language is decided once
per thread. A send gate checks the language, the subject, that numbers equal the decision,
the signature, the reply-by date and the asks. A refused email is recorded once and held for
the buyer.

## What the supplier said

`quote_extraction.v6` and `quotes/terms.py` read:
- lead time as value + unit + basis + approximate flag, including an offer-level default;
- validity given as a duration;
- whether a per-unit price applies to the requested quantity;
- general terms as offer notes, not line conditions;
- final-offer, declined-line and supplier-question signals.

Numbers inside identifiers ("cód. 000105", "MTR-100", "Pos. 2") are never accepted as
evidence for a value.

## Tests

- `evals.negotiation_scenarios`: 13 scripted multi-stage threads with step-by-step checks:
  - a replay of the real RFQ-6 thread;
  - concessions;
  - supplier questions;
  - partial quotes;
  - declines and alternatives;
  - no reply;
  - a stubborn supplier;
  - lead time only;
  - extras with out-of-order revisions;
  - German;
  - a reply while our request is pending;
  - above maximum;
  - answered clarifications.

  Run `--mock` for the harness and code, and `--live -k 3` with the real models.
- `evals.journey`: the whole buyer journey (analysis → RFQ → mandate → replies → negotiation
  → comparison). Its negotiation step checks business-rule properties rather than fixed
  sequences.
- Research behind the design: negotiation practice (Pactum, CIPS, PON, FAR 15.306), LLM
  negotiation studies, and business email practice, summarized in the pull request that
  introduced this design.
