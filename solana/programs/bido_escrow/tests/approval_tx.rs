//! Approval atomicity and transaction size (spec 01 section 3.9; T21, T22; ESC-AC-11, ESC-AC-12
//! Rust part): a gate approval is one transaction, so a failing `release` reverts the SAS
//! attestation created before it and every release of that approval; the `create_po` intake and
//! `approve_ppap` transactions, in their section 3.9 layouts as v0 messages with `CB×2` and two
//! signatures, fit one packet. CU and size are logged per transaction kind; only size is asserted.

mod common;

use {
    anchor_lang::{
        prelude::Pubkey, solana_program::instruction::error::InstructionError, AccountSerialize,
    },
    bido_escrow::{EscrowError, EscrowStatus, MilestoneStatus, PoEscrow, SAS_PROGRAM_ID},
    common::*,
    solana_signer::Signer,
    solana_transaction::versioned::VersionedTransaction,
    solana_transaction_error::TransactionError,
};

/// Index of `release(2)` in `[CB×2, CreateAttestation, release(1), release(2)]`.
const RELEASE_2_IX: u8 = 4;
/// Position of `payout_token` in `release`'s accounts.
const RELEASE_PAYOUT_TOKEN: usize = 2;

/// The account bytes `escrow` serializes to.
fn bytes(escrow: &PoEscrow) -> Vec<u8> {
    let mut out = Vec::new();
    escrow.try_serialize(&mut out).expect("serialize PoEscrow");
    out
}

/// The program id of each top-level instruction of `tx`, in order.
fn program_ids(tx: &VersionedTransaction) -> Vec<Pubkey> {
    let keys = tx.message.static_account_keys();
    tx.message
        .instructions()
        .iter()
        .map(|ix| keys[usize::from(ix.program_id_index)])
        .collect()
}

/// Logs the CU (by simulation) and the serialized size of `tx`, a transaction of kind `kind`, and
/// asserts it fits one packet.
fn measure(h: &Harness, kind: &str, tx: &VersionedTransaction) {
    println!(
        "tx kind: {kind}, {} account keys",
        tx.message.static_account_keys().len()
    );
    h.cu(tx);
    let bytes = size(tx);
    assert!(
        bytes <= PACKET_DATA_SIZE,
        "{kind}: {bytes} bytes > {PACKET_DATA_SIZE}"
    );
}

#[test]
fn t21_a_failing_release_reverts_the_whole_gate_approval() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    h.fund(&escrow);
    h.approve(&escrow, 0);
    let before = h.escrow(&escrow);
    let attestation = before.gates[1].attestation;
    let vault = h.ata(&escrow);
    let payout_token = h.ata(&h.payout_owner.pubkey());
    let vault_balance = h.token_balance(&vault);
    let payout_balance = h.token_balance(&payout_token);
    assert_eq!(before.milestones[1].status, MilestoneStatus::Pending);
    assert!(h.svm.get_account(&attestation).is_none());

    // The attacker's ATA of the same mint exists, so `release(2)` fails in Phase A.
    let attacker_token = h.mint_to(&test_keypair("attacker").pubkey(), 0);

    // Gate 1, `[CB×2, CreateAttestation, release(1), release(2)]`, with `release(2)` paying the
    // attacker: `release(1)` succeeds inside the transaction, `release(2)` fails.
    let mut ixs = compute_budget_ixs().to_vec();
    ixs.extend(h.approval_ixs(&escrow, 1, &h.sqe.pubkey(), &fixture_evidence_hash(1)));
    let release_2 = &mut ixs[usize::from(RELEASE_2_IX)];
    assert_eq!(release_2.program_id, bido_escrow::ID);
    assert_eq!(
        release_2.accounts[RELEASE_PAYOUT_TOKEN].pubkey,
        payout_token
    );
    release_2.accounts[RELEASE_PAYOUT_TOKEN].pubkey = attacker_token;
    let tx = h.tx(&ixs, &[&h.sqe]);
    assert_eq!(
        program_ids(&tx),
        [
            COMPUTE_BUDGET_PROGRAM_ID,
            COMPUTE_BUDGET_PROGRAM_ID,
            SAS_PROGRAM_ID,
            bido_escrow::ID,
            bido_escrow::ID,
        ]
    );
    let failed = h.expect_err(tx, EscrowError::PayoutMismatch);
    assert_eq!(
        failed.err,
        TransactionError::InstructionError(
            RELEASE_2_IX,
            InstructionError::Custom(EscrowError::PayoutMismatch.into())
        ),
        "the failure is release(2)'s"
    );

    // Nothing of the approval persists: no attestation, milestone 1 still `Pending`, no tokens moved.
    assert!(
        h.svm.get_account(&attestation).is_none(),
        "attestation account absent"
    );
    let after = h.escrow(&escrow);
    assert_eq!(after.milestones[1].status, MilestoneStatus::Pending);
    assert_eq!(after.milestones[2].status, MilestoneStatus::Pending);
    assert_eq!(bytes(&after), bytes(&before));
    assert_eq!(h.token_balance(&vault), vault_balance);
    assert_eq!(h.token_balance(&payout_token), payout_balance);
    assert_eq!(h.token_balance(&attacker_token), 0);

    // The gate's attestation address is still free, so the correct approval then succeeds.
    h.approve(&escrow, 1);
    assert_eq!(h.escrow(&escrow).status, EscrowStatus::Completed);
    assert_eq!(h.token_balance(&payout_token), TOTAL);
    assert_eq!(h.token_balance(&vault), 0);
}

#[test]
fn t22_create_po_and_approve_ppap_fit_one_packet() {
    let mut h = Harness::new();

    // `create_po` (intake): `[CB×2, create_po, Memo("bido:payout_ref:<hex>")]`.
    let args = h.create_po_args();
    let accounts = h.create_po_accounts(&args.chain_po_id);
    let escrow = accounts.escrow;
    let tx = h.create_po_intake_tx(accounts, args, &PAYOUT_REF);
    assert_eq!(
        program_ids(&tx),
        [
            COMPUTE_BUDGET_PROGRAM_ID,
            COMPUTE_BUDGET_PROGRAM_ID,
            bido_escrow::ID,
            MEMO_PROGRAM_ID,
        ]
    );
    assert_eq!(tx.signatures.len(), 2, "relayer and operator");
    let memo = &tx.message.instructions()[3].data;
    assert_eq!(memo.len(), PAYOUT_REF_MEMO_PREFIX.len() + 64);
    assert!(memo.starts_with(PAYOUT_REF_MEMO_PREFIX.as_bytes()));
    measure(&h, "create_po", &tx);
    h.send_ok(tx);
    assert_eq!(h.escrow(&escrow).status, EscrowStatus::Created);

    // `fund` (Reserve): `[CB×2, fund]`.
    let buyer = h.buyer.pubkey();
    h.mint_to(&buyer, TOTAL);
    let mut ixs = compute_budget_ixs().to_vec();
    ixs.push(fund_ix(h.fund_accounts(&escrow, &buyer)));
    let tx = h.tx(&ixs, &[&h.buyer]);
    assert_eq!(tx.signatures.len(), 2, "relayer and buyer");
    measure(&h, "fund", &tx);
    h.send_ok(tx);

    // `release_tool_start`: `[CB×2, CreateAttestation(gate 0), release(0)]`.
    let tx = h.approve_tx(&escrow, 0, &h.buyer, &fixture_evidence_hash(0));
    assert_eq!(tx.signatures.len(), 2, "relayer and buyer");
    measure(&h, "release_tool_start", &tx);
    h.send_ok(tx);

    // `approve_ppap`: `[CB×2, CreateAttestation(gate 1), release(1), release(2)]`.
    let tx = h.approve_tx(&escrow, 1, &h.sqe, &fixture_evidence_hash(1));
    assert_eq!(
        program_ids(&tx),
        [
            COMPUTE_BUDGET_PROGRAM_ID,
            COMPUTE_BUDGET_PROGRAM_ID,
            SAS_PROGRAM_ID,
            bido_escrow::ID,
            bido_escrow::ID,
        ]
    );
    assert_eq!(tx.signatures.len(), 2, "relayer and SQE");
    measure(&h, "approve_ppap", &tx);
    h.send_ok(tx);
    assert_eq!(h.escrow(&escrow).status, EscrowStatus::Completed);
}
