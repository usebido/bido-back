"""Release a funded PO escrow milestone by milestone: Tool start, T1, PPAP.

    buyer:   POST /v1/purchase-orders/{id}/payment/factory-link ──► the factory's link
    factory: GET  /v1/factory/order (X-Factory-Token), then per milestone:
             POST /v1/factory/order/milestones/{index}/documents   (one file per call)
             POST /v1/factory/order/milestones/{index}/submit ──► pending approval
                  (`release_po_milestone`); a buyer may gather and submit on its behalf
    person:  POST /v1/approvals/{id}/decision ──► the worker runs the act once:
             claim the milestone (`releasing`) ──► `milestone.release` job:
             [CU, CU, SAS CreateAttestation(gate), release(milestone)] signed by the
             relayer and the gate attester ──► `released`; the last one completes the
             payment. A rejection (or POST .../request-changes with a reason) sends the
             milestone back to the factory.

* The catalog (`catalog`) fixes the tooling plan: Tool start 30%, T1 30%, PPAP 40%,
  each released through its own gate, so three releases are three transactions.
* Strictly in order: a milestone can be submitted once the escrow is funded and every
  earlier milestone is released (`state.submittable`).
* What is approved is exactly what is released: the submission's files, hashed into
  the evidence hash the attestation carries on chain, the amount fixed by the escrow,
  and the payout fixed at `create_po`. The act and the job re-check them each time.
* Exactly once: the milestone row is the outbox; every transaction is recorded before
  it is sent and a retry asks the chain first (`release`).
* The factory never sees chain words, the escrow or the buyer's other data: only its
  order, the amounts and what to send.
"""
