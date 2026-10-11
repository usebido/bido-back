"""Pay an issued purchase order by card, mint Bido's stablecoin and lock it in the PO's escrow.

    buyer: POST /v1/purchase-orders/{id}/payment ──► pending approval (`pay_purchase_order`)
    person: POST /v1/approvals/{id}/decision, or Basis Theory verification on `vic`,
        ──► the worker runs the act once:
        Basis Theory allowance (`spt` rail) ─► single-use Stripe token ─► Stripe charge
    worker, one step per `payment.advance` job, each idempotent:
        mint the stablecoin into the company's Privy wallet (relayer pays, Bido's mint
        authority signs) ─► `create_po` on `bido_escrow` (operator signs) ─► `fund`
        (the company's wallet signs through Privy) ─► the escrow holds the PO total.

Then `payments.milestones` releases the escrow milestone by milestone (Tool start, T1,
PPAP): the factory sends each milestone's documents through its link, a person approves
the release, and the worker writes the gate's Solana Attestation Service attestation and
releases that milestone, once. Next steps, not here: refunds, and the payout of each
released milestone in the supplier's currency.

* Nothing commits without a person: paying is an `act` behind the approval guard
  (`payments.execute`). On `vic`, the cardholder's Basis Theory passkey verification
  decides the approval. Every later step only carries out what was approved (amount,
  card, payout account, milestones, escrow id), re-checked each time.
* The card never reaches Bido: Basis Theory Elements tokenize it in the browser; the
  Stripe token is minted and charged inside one call and never stored.
* The supplier's bank account is sealed in Bido's database (`payments.bank_details`,
  KMS in AWS) until the payout provider that pays it (Tazapay, next step) holds it as a
  beneficiary; in clear Bido keeps a masked view and the fingerprint the payment is
  bound to (written on chain as the `bido:payout_ref:` memo of `create_po`). Basis
  Theory is used for cards only.
* Exactly once: the card charge uses the payment id as idempotency key at Basis
  Theory and Stripe; every Solana transaction is recorded (signature and last valid
  block height) before it is sent, and a retry asks the chain first: it rebuilds
  only after the old transaction can no longer land, and `create_po`/`fund` are
  idempotent on chain (the escrow PDA exists once, it is funded once).
* Providers stay behind `payments.ports`; their types never leave
  `payments.providers` and `payments.chain`.
"""
