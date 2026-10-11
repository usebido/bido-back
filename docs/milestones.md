# Milestone releases: Tool start, T1, PPAP

How a purchase order's escrow pays the factory for a new tool, one milestone at a time. The
PO payment (`bido_back.payments`) charges the buyer's card, mints Bido's stablecoin and locks
the PO total in the order's Solana escrow (`bido_escrow`). From there, the money moves only
when the factory proves a milestone and a person at the buyer approves it.

## The tooling plan

| # | Milestone | On chain | Share | Released when | The factory must send |
|---|---|---|---|---|---|
| 0 | Tool start (模具启动) | `Tool start` | 30% | the design is frozen and the tool steel is at the factory, committed to this tool | photos of the steel marked with the tool ID and PO number; the steel certificate |
| 1 | T1 samples (T1 试模样件) | `T1` | 30% | the tool is complete and runs: first samples molded and measured, every deviation on a correction plan | the T1 trial report with the dimensional results; sample photos |
| 2 | PPAP approval (PPAP 批准) | `PPAP` | 40% | proven at scale: a production run on the tool (300+ consecutive parts at the quoted rate) made parts that meet every requirement | signed PSW; full dimensional report, every cavity; run-at-rate report |

Each milestone also takes optional evidence: the DFM report, mold design and tooling schedule
at Tool start; a trial video, mold photos, the issue list with the correction plan and the
sample waybill at T1; the capability study (Ppk), material certificate, deviation request and
photos at PPAP. An interim PPAP approval is not an approval: the buyer asks for changes with
the reason `ppap_interim_only` and the PPAP share stays locked. The catalog is `bido_back.payments.milestones.catalog`: one table, never
hard-coded elsewhere.

- **Three transactions.** `create_po` gives milestone *i* its own gate *i*, so each release is
  its own transaction: `[SetComputeUnitLimit, SetComputeUnitPrice, SAS CreateAttestation(gate
  i), release(i)]`. Tool start pays 30% of the total first; T1 and PPAP pay the rest later.
- **The plan is the default** when a payment is requested without milestones. A PO may use
  other shares (30/40/30 is common with Chinese mold makers) by sending the milestones with
  their catalog `key` and its own `bps`; a custom milestone without a key asks for one piece of
  evidence.
- **T2, T3...** are new attempts at the T1 milestone after the buyer asks for changes, never
  new milestones.

## Flow

```
buyer    POST /v1/purchase-orders/{po}/payment               the plan above, by default
worker   charge -> mint -> create_po -> fund                  the escrow holds the total
buyer    POST /v1/purchase-orders/{po}/payment/factory-link   {token, url}, shown once
factory  opens <FACTORY_PAGE_URL>#<token>; the page calls the API with X-Factory-Token
factory  GET  /v1/factory/order                               order, milestones, what to send
factory  POST /v1/factory/order/accept                        confirms the order, once
factory  POST /v1/factory/order/milestones/{i}/documents?doc_type=&filename=   one file
factory  POST /v1/factory/order/milestones/{i}/submit         -> approval release_po_milestone
buyer    GET  /v1/approvals?status=pending                    milestone_release: files, checks
buyer    POST /v1/approvals/{id}/decision {approve}           or .../request-changes {reason}
worker   approval.execute -> milestone.release                attestation + release, once
buyer    GET  /v1/purchase-orders/{po}/payment/milestones      status, files, release signature
buyer    POST .../milestones/{i}/retry-release                 a failed release, once fixed
```

The payment's state stays `funded` throughout (the API's payment states are unchanged): the
purchase order shows `released_bps` and `next_milestone` ("30% paid · next: T1"), and the
payment's `completed_at` once the escrow released every milestone. Each release's signature
and explorer link are on its milestone.

- **Strictly in order.** The factory may gather documents for any open milestone of a funded
  escrow, but a milestone is submitted, approved and released only after every earlier one is
  released. The act that claims the release checks the order again.
- **A person decides every release.** The approval's requester is the person who requested the
  payment; the guard's rules apply unchanged (self-approval by default, four-eyes when the
  company requires it). Nothing is approved on silence, and negotiation mandates never cover
  releases.
- **What is approved is what is released.** The submission's files are hashed into
  `evidence_hash = sha256("bido-evidence-v1" ‖ chain_po_id ‖ u8 gate ‖ sorted file sha256s)`,
  which the gate's attestation carries on chain and the escrow keeps on the released milestone.
  The act and the release job recompute it from the stored files and refuse any change.
- **Changes requested.** `request-changes` rejects the approval with a reason code and text; the
  milestone opens again and the factory's next draft starts with the files it already sent, so
  it replaces only what was refused.
- **Exactly once.** The milestone row is the outbox. Each release transaction is recorded
  before it is sent; a retry asks the chain first (milestone already released? attestation
  already written? then only `release`), and never builds while an earlier transaction can
  still land. A stuck release is woken by the scheduled sweep.
- **Who signs.** Bido's gate attester (`SOLANA_GATE_ATTESTER_KEY`, pinned on every gate at
  `create_po`) signs the attestation after the guard approved; the relayer pays fees and rent.
  The approval row is the authority; the chain proves that Bido attested this evidence for this
  gate of this PO.

## The factory's link

- A random 256-bit token; Bido stores only its sha256. The link is the factory page's URL with
  the token in the fragment (`#<token>`), so it never reaches a server log; the page sends it in
  the `X-Factory-Token` header. One active link per payment; issuing a new one revokes the old
  one. It expires after `FACTORY_LINK_TTL_DAYS` (90). Unknown, revoked and expired tokens get
  the same 401.
- The factory sees its order, the amounts in the order's currency and what to send. Never the
  escrow, the chain, wallets, hashes or the buyer's other data.
- Uploads: one file per request, its kind read from its first bytes (PDF, JPEG, PNG, WebP,
  XLSX, MP4/MOV), at most `MILESTONE_DOCUMENT_MAX_BYTES` (20 MiB). The same file twice is
  stored once.

## Operations

- Set `SOLANA_GATE_ATTESTER_KEY` (the secret of `SOLANA_GATE_ATTESTER`, an authorized signer
  of the SAS credential) on the worker. Without it, milestones cannot be submitted (503).
  Every escrow pins the attester it was created with: escrows created so far on devnet pin the
  current `SOLANA_GATE_ATTESTER`, so its key must stay available until they are released. The
  devnet program is an early build whose `create_po` does not check that the attester may sign
  under the credential; Bido checks it before `create_po` instead, because an escrow pinned to
  an attester the credential does not list can never be released.
- Set `FACTORY_PAGE_URL` to the factory page of the web app; without it the API returns the
  token only.
- `PAYMENTS_ESCROW_REFUND_DAYS` (365 by default; it was 90) is when the buyer could take back
  the unreleased balance, cancelling every milestone not yet released. A tooling program often
  reaches PPAP after 90 days: keep it past the planned PPAP date.

## Not yet

- The payout of each released milestone to the factory's bank (Tazapay) and its accounting.
- Notifications (the approver when a milestone is submitted, the factory when it is released
  or sent back), withdrawing a submission, putting a milestone on hold, and refunds.
- A quality approver role per milestone (the SQE for T1 and PPAP): today any approver decides.
