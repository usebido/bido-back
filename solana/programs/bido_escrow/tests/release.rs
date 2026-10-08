//! `release` (spec 01 section 3.6, the approval layout of section 3.9, section 3.8
//! `MilestoneReleased` and `PoCompleted`; T01-T05, T07; ESC-AC-09, ESC-AC-11 part): each gate's
//! approval is one transaction that creates the SAS attestation and releases that gate's
//! milestones against it, paying exactly the PO total to the fixed payout and persisting who
//! approved, the evidence and the slot. A release without an attestation, with another PO's
//! attestation, with one signed by an authorised signer who is not the gate's attester, of an
//! already released milestone, or paying any account but the payout token fails with its own
//! error and moves nothing. T15's "release before fund" case is in `fund.rs`. Release semantics
//! (T19, T20, T25; ESC-AC-14): the last milestone takes the rounding remainder, so the releases
//! empty the vault; a gate's milestones may be cranked out of order; and the second allowed mint
//! of `Config` runs the same happy path as tUSD.

mod common;

use {
    anchor_lang::{
        prelude::Pubkey,
        solana_program::{instruction::error::InstructionError, program_option::COption},
        AccountDeserialize, AccountSerialize,
    },
    anchor_spl::{associated_token::get_associated_token_address, token},
    bido_escrow::{
        EscrowError, EscrowStatus, MilestoneReleased, MilestoneStatus, PoCompleted, PoEscrow,
        SAS_PROGRAM_ID,
    },
    common::*,
    solana_signer::Signer,
    solana_transaction::versioned::VersionedTransaction,
    solana_transaction_error::TransactionError,
};

/// Slots at which the fixture approvals land, so each milestone's `released_slot` is distinct.
const GATE_0_SLOT: u64 = 1_000;
const GATE_1_SLOT: u64 = 2_000;
/// T02's escrow B: another PO of the same buyer, payout, credential and schema.
const OTHER_PO_ID: &str = "PO-1043-9WXR";
/// Index of the second `release(1)` in `[CB×2, CreateAttestation, release(1), release(1)]`.
const SECOND_RELEASE_IX: u8 = 4;
/// T19's PO: a total that its bps split cannot divide evenly.
const ROUNDING_TOTAL: u64 = 1_000_001;
const ROUNDING_BPS: [u16; 3] = [3333, 3333, 3334];
/// T25's second allowed mint (`test_keypair` label).
const CIRCLE_LIKE_MINT: &str = "circle-like-usdc-mint";

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

/// `stored` after `release(milestone)` in `slot` against an approval by `approver`: only the
/// milestone's status, slot, approver and evidence, `released` and possibly `status` change (I5).
fn released(
    mut stored: PoEscrow,
    milestone: usize,
    slot: u64,
    approver: Pubkey,
    evidence_hash: [u8; 32],
) -> PoEscrow {
    let m = &mut stored.milestones[milestone];
    m.status = MilestoneStatus::Released;
    m.released_slot = slot;
    m.approved_by = approver;
    m.evidence_hash = evidence_hash;
    stored.released += m.amount;
    if stored.released == stored.total {
        stored.status = EscrowStatus::Completed;
    }
    stored
}

#[test]
fn t07_gate_0_then_gate_1_release_the_whole_total_to_the_payout() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    h.fund(&escrow);
    let funded = h.escrow(&escrow);
    let vault = h.ata(&escrow);
    let payout_token = h.ata(&h.payout_owner.pubkey());
    let buyer = h.buyer.pubkey();
    let sqe = h.sqe.pubkey();
    assert_eq!(funded.status, EscrowStatus::Funded);
    assert_eq!(funded.payout_token, payout_token);
    assert_eq!(h.token_balance(&vault), TOTAL);
    assert_eq!(h.token_balance(&payout_token), 0);
    let [tool_start, t1, ppap] = [0, 1, 2].map(|m: usize| funded.milestones[m].amount);

    // Gate 0: the buyer approves Tool start; `[CB×2, CreateAttestation, release(0)]`.
    h.svm.warp_to_slot(GATE_0_SLOT);
    let evidence_0 = fixture_evidence_hash(0);
    let tx = h.approve_tx(&escrow, 0, &h.buyer, &evidence_0);
    assert_eq!(
        program_ids(&tx),
        [
            COMPUTE_BUDGET_PROGRAM_ID,
            COMPUTE_BUDGET_PROGRAM_ID,
            SAS_PROGRAM_ID,
            bido_escrow::ID,
        ]
    );
    assert_eq!(tx.signatures.len(), 2, "relayer and buyer");
    h.cu(&tx);
    size(&tx);
    let meta = h.send_ok(tx.clone());

    assert_eq!(h.token_balance(&payout_token), tool_start);
    assert_eq!(h.token_balance(&vault), TOTAL - tool_start);
    let after_gate_0 = released(funded.clone(), 0, GATE_0_SLOT, buyer, evidence_0);
    assert_eq!(after_gate_0.status, EscrowStatus::Funded);
    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&after_gate_0));

    let attestation_0 = h
        .svm
        .get_account(&funded.gates[0].attestation)
        .expect("gate 0 attestation exists");
    assert_eq!(attestation_0.owner, SAS_PROGRAM_ID);
    let decoded = GateAttestation::decode(&attestation_0.data);
    assert_eq!(decoded.signer, buyer);
    assert_eq!(decoded.gate, 0);
    assert_eq!(decoded.evidence_hash, evidence_0);

    let events: Vec<MilestoneReleased> = emitted(&tx, &meta);
    assert_eq!(events.len(), 1, "one MilestoneReleased");
    let event = &events[0];
    assert_eq!(event.escrow, escrow);
    assert_eq!(event.chain_po_id, PO_ID);
    assert_eq!((event.milestone, event.gate), (0, 0));
    assert_eq!(event.amount, tool_start);
    assert_eq!(event.released_total, tool_start);
    assert_eq!(event.payout, h.payout_owner.pubkey());
    assert_eq!(event.payout_token, payout_token);
    assert_eq!(event.attestation, funded.gates[0].attestation);
    assert_eq!(event.approved_by, buyer);
    assert_eq!(event.evidence_hash, evidence_0);
    assert_eq!(event.slot, GATE_0_SLOT);
    assert!(emitted::<PoCompleted>(&tx, &meta).is_empty());

    // Gate 1: the SQE approves T1 and PPAP; `[CB×2, CreateAttestation, release(1), release(2)]`.
    h.svm.warp_to_slot(GATE_1_SLOT);
    let evidence_1 = fixture_evidence_hash(1);
    let tx = h.approve_tx(&escrow, 1, &h.sqe, &evidence_1);
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
    h.cu(&tx);
    size(&tx);
    let meta = h.send_ok(tx.clone());

    // The payout received exactly the total and the vault is empty.
    assert_eq!(h.token_balance(&payout_token), TOTAL);
    assert_eq!(h.token_balance(&vault), 0);
    let completed = released(
        released(after_gate_0, 1, GATE_1_SLOT, sqe, evidence_1),
        2,
        GATE_1_SLOT,
        sqe,
        evidence_1,
    );
    let stored = h.escrow(&escrow);
    assert_eq!(stored.status, EscrowStatus::Completed);
    assert_eq!(stored.released, TOTAL);
    assert_eq!(bytes(&stored), bytes(&completed));

    let events: Vec<MilestoneReleased> = emitted(&tx, &meta);
    let shape: Vec<_> = events
        .iter()
        .map(|e| (e.milestone, e.gate, e.amount, e.released_total))
        .collect();
    assert_eq!(
        shape,
        [(1, 1, t1, tool_start + t1), (2, 1, ppap, TOTAL)],
        "MilestoneReleased for T1, then PPAP"
    );
    for event in &events {
        assert_eq!(event.attestation, funded.gates[1].attestation);
        assert_eq!(event.approved_by, sqe);
        assert_eq!(event.evidence_hash, evidence_1);
        assert_eq!(event.slot, GATE_1_SLOT);
    }
    let events: Vec<PoCompleted> = emitted(&tx, &meta);
    assert_eq!(events.len(), 1, "one PoCompleted");
    assert_eq!(events[0].escrow, escrow);
    assert_eq!(events[0].chain_po_id, PO_ID);
    assert_eq!(events[0].total, TOTAL);
    assert_eq!(events[0].slot, GATE_1_SLOT);
}

#[test]
fn t01_release_without_an_attestation_fails_with_attestation_missing() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    h.fund(&escrow);
    let funded = h.escrow(&escrow);
    let vault = h.ata(&escrow);
    let payout_token = h.ata(&h.payout_owner.pubkey());

    // Nobody approved: each milestone's expected attestation address holds no account.
    for (milestone, m) in (0u8..).zip(&funded.milestones) {
        println!("case: release({milestone})");
        let attestation = funded.gates[usize::from(m.gate)].attestation;
        assert!(h.svm.get_account(&attestation).is_none());
        let tx = h.release_with(&escrow, milestone, &attestation);
        h.expect_err(tx, EscrowError::AttestationMissing);
    }

    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&funded));
    assert_eq!(h.token_balance(&vault), TOTAL);
    assert_eq!(h.token_balance(&payout_token), 0);
}

#[test]
fn t02_another_pos_attestation_fails_with_attestation_address_mismatch() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    h.fund(&escrow);
    let other = h.create_po_with_id(OTHER_PO_ID);
    h.fund(&other);
    h.approve(&other, 1);
    let funded = h.escrow(&escrow);
    let vault = h.ata(&escrow);
    let payout_token = h.ata(&h.payout_owner.pubkey());
    let payout_balance = h.token_balance(&payout_token);

    // Escrow B's gate-1 attestation is real and matches escrow A's gate 1 in every field but the
    // nonce: same credential, schema, `po_hash`, gate and attester.
    let foreign = h.escrow(&other).gates[1].attestation;
    assert_ne!(foreign, funded.gates[1].attestation);
    let account = h
        .svm
        .get_account(&foreign)
        .expect("escrow B's gate-1 attestation exists");
    assert_eq!(account.owner, SAS_PROGRAM_ID);
    let decoded = GateAttestation::decode(&account.data);
    assert_eq!(decoded.nonce, gate_nonce(&other, 1));
    assert_eq!(decoded.credential, funded.credential);
    assert_eq!(decoded.schema, funded.schema);
    assert_eq!(decoded.po_hash, funded.po_hash);
    assert_eq!(decoded.gate, 1);
    assert_eq!(decoded.signer, funded.gates[1].attester);

    for milestone in [1, 2] {
        println!("case: release({milestone}) of escrow A with escrow B's gate-1 attestation");
        let tx = h.release_with(&escrow, milestone, &foreign);
        h.expect_err(tx, EscrowError::AttestationAddressMismatch);
    }

    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&funded));
    assert_eq!(h.token_balance(&vault), TOTAL);
    assert_eq!(h.token_balance(&payout_token), payout_balance);
}

#[test]
fn t03_an_attestation_by_another_authorised_signer_fails_with_attester_mismatch() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    h.fund(&escrow);
    let funded = h.escrow(&escrow);
    let buyer = h.buyer.pubkey();
    let attestation = funded.gates[1].attestation;
    let vault = h.ata(&escrow);
    let payout_token = h.ata(&h.payout_owner.pubkey());

    // The buyer is an authorised signer of the credential, so SAS lets it attest any gate, but the
    // attester pinned for gate 1 is the SQE.
    let credential = h.svm.get_account(&h.credential).expect("credential exists");
    assert!(SasCredential::decode(&credential.data)
        .authorized_signers
        .contains(&buyer));
    assert_eq!(funded.gates[1].attester, h.sqe.pubkey());

    // In the approval layout: SAS writes the buyer's attestation, `release(1)` rejects it and the
    // whole transaction reverts.
    println!("case: gate-1 approval signed by the buyer");
    let tx = h.approve_tx(&escrow, 1, &h.buyer, &fixture_evidence_hash(1));
    h.expect_err(tx, EscrowError::AttesterMismatch);
    assert!(h.svm.get_account(&attestation).is_none());

    // Squatted: the buyer's attestation alone persists at gate 1's address, correct in every field
    // but the signer, and no release accepts it.
    let tx = h.attest_tx(&escrow, 1, &h.buyer, &fixture_evidence_hash(1));
    h.send_ok(tx);
    let account = h
        .svm
        .get_account(&attestation)
        .expect("the buyer's attestation squats gate 1's address");
    assert_eq!(account.owner, SAS_PROGRAM_ID);
    let decoded = GateAttestation::decode(&account.data);
    assert_eq!(decoded.nonce, gate_nonce(&escrow, 1));
    assert_eq!(decoded.credential, funded.credential);
    assert_eq!(decoded.schema, funded.schema);
    assert_eq!(decoded.po_hash, funded.po_hash);
    assert_eq!(decoded.gate, 1);
    assert_eq!(decoded.signer, buyer);
    for milestone in [1, 2] {
        println!("case: release({milestone}) with the squatted attestation");
        let tx = h.release_with(&escrow, milestone, &attestation);
        h.expect_err(tx, EscrowError::AttesterMismatch);
    }

    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&funded));
    assert_eq!(h.token_balance(&vault), TOTAL);
    assert_eq!(h.token_balance(&payout_token), 0);
}

#[test]
fn t04_the_same_milestone_twice_fails_with_milestone_already_released() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    h.fund(&escrow);
    h.approve(&escrow, 0);
    let after_gate_0 = h.escrow(&escrow);
    let tool_start = after_gate_0.milestones[0].amount;
    let vault = h.ata(&escrow);
    let payout_token = h.ata(&h.payout_owner.pubkey());
    assert_eq!(after_gate_0.milestones[0].status, MilestoneStatus::Released);
    assert_eq!(h.token_balance(&payout_token), tool_start);

    // In two transactions: gate 0's approval released Tool start; releasing it again fails.
    println!("case: release(0) in a second transaction");
    let tx = h.release_with(&escrow, 0, &after_gate_0.gates[0].attestation);
    h.expect_err(tx, EscrowError::MilestoneAlreadyReleased);

    // Inside one transaction: gate 1's approval with `release(1)` twice; the second fails and the
    // whole approval reverts.
    println!("case: release(1) twice in one approval");
    let attestation = after_gate_0.gates[1].attestation;
    let mut ixs = compute_budget_ixs().to_vec();
    ixs.push(h.attestation_ix(&escrow, 1, &h.sqe.pubkey(), &fixture_evidence_hash(1)));
    let release_1 = release_ix(h.release_accounts(&escrow, &attestation), 1);
    ixs.extend([release_1.clone(), release_1]);
    let tx = h.tx(&ixs, &[&h.sqe]);
    let failed = h.expect_err(tx, EscrowError::MilestoneAlreadyReleased);
    assert_eq!(
        failed.err,
        TransactionError::InstructionError(
            SECOND_RELEASE_IX,
            InstructionError::Custom(EscrowError::MilestoneAlreadyReleased.into())
        ),
        "the failure is the second release(1)'s"
    );
    assert!(h.svm.get_account(&attestation).is_none());

    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&after_gate_0));
    assert_eq!(h.token_balance(&vault), TOTAL - tool_start);
    assert_eq!(h.token_balance(&payout_token), tool_start);

    // Each milestone paid once: the correct gate-1 approval completes the PO with exactly the total.
    h.approve(&escrow, 1);
    assert_eq!(h.escrow(&escrow).status, EscrowStatus::Completed);
    assert_eq!(h.token_balance(&payout_token), TOTAL);
    assert_eq!(h.token_balance(&vault), 0);
}

#[test]
fn t05_paying_another_account_fails_with_payout_mismatch() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    h.fund(&escrow);
    // Gate 0's real attestation exists, so anyone may crank `release(0)` with it.
    let attestation = h.attest(&escrow, 0);
    let account = h.svm.get_account(&attestation).expect("gate 0 attestation");
    assert_eq!(account.owner, SAS_PROGRAM_ID);
    assert_eq!(
        GateAttestation::decode(&account.data).signer,
        h.buyer.pubkey()
    );
    let funded = h.escrow(&escrow);
    let tool_start = funded.milestones[0].amount;
    let vault = h.ata(&escrow);
    let payout = h.payout_owner.pubkey();
    let payout_token = h.ata(&payout);
    assert_eq!(funded.payout_token, payout_token);

    let attacker_token = h.mint_to(&test_keypair("attacker").pubkey(), 0);
    let other_mint = h.new_mint("other-mint");
    let other_mint_token = h.create_ata(&payout, &other_mint);
    for (case, wrong) in [
        ("the attacker's ATA of the same mint", attacker_token),
        ("the payout's ATA of another mint", other_mint_token),
    ] {
        println!("case: release(0) paying {case}");
        let mut accounts = h.release_accounts(&escrow, &attestation);
        accounts.payout_token = wrong;
        let mut ixs = compute_budget_ixs().to_vec();
        ixs.push(release_ix(accounts, 0));
        let tx = h.tx(&ixs, &[]);
        h.expect_err(tx, EscrowError::PayoutMismatch);
        assert_eq!(h.token_balance(&wrong), 0);
    }

    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&funded));
    assert_eq!(h.token_balance(&vault), TOTAL);
    assert_eq!(h.token_balance(&payout_token), 0);

    // The same crank to the stored payout token pays exactly Tool start.
    let tx = h.release_with(&escrow, 0, &attestation);
    h.send_ok(tx);
    assert_eq!(h.token_balance(&payout_token), tool_start);
    assert_eq!(h.token_balance(&vault), TOTAL - tool_start);
    assert_eq!(
        h.escrow(&escrow).milestones[0].status,
        MilestoneStatus::Released
    );
}

#[test]
fn t19_the_last_milestone_takes_the_rounding_remainder_and_the_vault_ends_at_0() {
    let mut h = Harness::new();
    let mut args = h.create_po_args();
    args.total = ROUNDING_TOTAL;
    for (milestone, bps) in args.milestones.iter_mut().zip(ROUNDING_BPS) {
        milestone.bps = bps;
    }
    let accounts = h.create_po_accounts(&args.chain_po_id);
    let escrow = accounts.escrow;
    let tx = h.create_po_tx(accounts, args);
    h.send_ok(tx);

    // floor(1,000,001 × 3333 / 10,000) = 333,300 for Tool start and T1; PPAP takes the rest,
    // 1,000,001 − 666,600 = 333,401, one base unit more than floor(1,000,001 × 3334 / 10,000).
    let created = h.escrow(&escrow);
    assert_eq!(created.total, ROUNDING_TOTAL);
    let amounts: Vec<u64> = created.milestones.iter().map(|m| m.amount).collect();
    assert_eq!(amounts, [333_300, 333_300, 333_401]);
    assert_eq!(amounts.iter().sum::<u64>(), ROUNDING_TOTAL);

    h.fund(&escrow);
    let vault = h.ata(&escrow);
    let payout_token = h.ata(&h.payout_owner.pubkey());
    assert_eq!(h.token_balance(&vault), ROUNDING_TOTAL);

    h.approve(&escrow, 0);
    assert_eq!(h.token_balance(&payout_token), 333_300);
    assert_eq!(h.token_balance(&vault), ROUNDING_TOTAL - 333_300);

    h.approve(&escrow, 1);
    let stored = h.escrow(&escrow);
    assert_eq!(stored.status, EscrowStatus::Completed);
    assert_eq!(stored.released, ROUNDING_TOTAL);
    assert_eq!(h.token_balance(&payout_token), ROUNDING_TOTAL);
    assert_eq!(h.token_balance(&vault), 0);
}

#[test]
fn t20_release_2_before_release_1_in_the_release_attempt_shape_succeeds() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    h.fund(&escrow);
    // Gate 1's attestation on its own, with no release: what a `release_attempt` cranks.
    let attestation = h.attest(&escrow, 1);
    let funded = h.escrow(&escrow);
    assert_eq!(attestation, funded.gates[1].attestation);
    let [tool_start, t1, ppap] = [0, 1, 2].map(|m: usize| funded.milestones[m].amount);
    let vault = h.ata(&escrow);
    let payout_token = h.ata(&h.payout_owner.pubkey());
    let (buyer, sqe) = (h.buyer.pubkey(), h.sqe.pubkey());
    let evidence_1 = fixture_evidence_hash(1);
    let [ppap_slot, t1_slot, tool_start_slot] = [1_000, 2_000, 3_000];

    // `[CB×2, release(2)]`, signed by the relayer only: PPAP is paid while T1 is still pending.
    h.svm.warp_to_slot(ppap_slot);
    let tx = h.release_with(&escrow, 2, &attestation);
    assert_eq!(
        program_ids(&tx),
        [
            COMPUTE_BUDGET_PROGRAM_ID,
            COMPUTE_BUDGET_PROGRAM_ID,
            bido_escrow::ID,
        ]
    );
    assert_eq!(tx.signatures.len(), 1, "relayer only");
    let meta = h.send_ok(tx.clone());
    let after_ppap = released(funded, 2, ppap_slot, sqe, evidence_1);
    assert_eq!(after_ppap.milestones[1].status, MilestoneStatus::Pending);
    assert_eq!(after_ppap.status, EscrowStatus::Funded);
    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&after_ppap));
    assert_eq!(h.token_balance(&payout_token), ppap);
    assert_eq!(h.token_balance(&vault), TOTAL - ppap);
    let events: Vec<MilestoneReleased> = emitted(&tx, &meta);
    let shape: Vec<_> = events
        .iter()
        .map(|e| (e.milestone, e.gate, e.amount, e.released_total))
        .collect();
    assert_eq!(shape, [(2, 1, ppap, ppap)], "MilestoneReleased for PPAP");

    // `release(1)` afterwards, against the same attestation.
    h.svm.warp_to_slot(t1_slot);
    let tx = h.release_with(&escrow, 1, &attestation);
    let meta = h.send_ok(tx.clone());
    let after_t1 = released(after_ppap, 1, t1_slot, sqe, evidence_1);
    assert_eq!(after_t1.status, EscrowStatus::Funded);
    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&after_t1));
    assert_eq!(h.token_balance(&payout_token), ppap + t1);
    assert_eq!(h.token_balance(&vault), TOTAL - ppap - t1);
    let events: Vec<MilestoneReleased> = emitted(&tx, &meta);
    let shape: Vec<_> = events
        .iter()
        .map(|e| (e.milestone, e.gate, e.amount, e.released_total))
        .collect();
    assert_eq!(shape, [(1, 1, t1, ppap + t1)], "MilestoneReleased for T1");
    assert!(emitted::<PoCompleted>(&tx, &meta).is_empty());

    // Gate 0's approval, last, completes the PO with exactly the total.
    h.svm.warp_to_slot(tool_start_slot);
    h.approve(&escrow, 0);
    let completed = released(
        after_t1,
        0,
        tool_start_slot,
        buyer,
        fixture_evidence_hash(0),
    );
    assert_eq!(completed.status, EscrowStatus::Completed);
    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&completed));
    assert_eq!(h.token_balance(&payout_token), TOTAL);
    assert_eq!(h.token_balance(&vault), 0);
    assert_eq!(tool_start + t1 + ppap, TOTAL);
}

#[test]
fn t25_the_second_allowed_mint_runs_the_happy_path() {
    let mut h = Harness::without_config();
    let tusd = h.mint;
    let treasury = h.treasury.pubkey();
    let circle_like = h.new_circle_like_mint(CIRCLE_LIKE_MINT);
    let account = h
        .svm
        .get_account(&circle_like)
        .expect("Circle-like mint exists");
    assert_eq!(account.owner, token::ID, "a classic SPL Token mint");
    let mint = token::Mint::try_deserialize(&mut account.data.as_slice()).expect("mint");
    assert_eq!(mint.decimals, MINT_DECIMALS);
    assert_eq!(mint.mint_authority, COption::Some(treasury));
    assert_eq!(mint.freeze_authority, COption::Some(treasury));

    // `Config` allows `[tUSD, Circle-like]`, as `chain-setup.ts config` writes it on devnet.
    let mut args = h.init_config_args();
    args.allowed_mints = [tusd, circle_like];
    let ix = init_config_ix(&h.admin.pubkey(), args, &h.credential, &h.schema);
    let tx = h.tx(&[ix], &[&h.admin]);
    h.send_ok(tx);
    assert_eq!(h.config().allowed_mints, [tusd, circle_like]);

    // The fixture PO in the second mint: the harness helpers create, fund and pay in `h.mint`.
    h.mint = circle_like;
    let escrow = h.create_po();
    h.fund(&escrow);
    let funded = h.escrow(&escrow);
    let payout = h.payout_owner.pubkey();
    let vault = get_associated_token_address(&escrow, &circle_like);
    let payout_token = get_associated_token_address(&payout, &circle_like);
    assert_eq!(funded.status, EscrowStatus::Funded);
    assert_eq!(funded.mint, circle_like);
    assert_eq!((funded.vault, funded.payout_token), (vault, payout_token));
    assert_eq!(h.token_balance(&vault), TOTAL);

    h.approve(&escrow, 0);
    assert_eq!(h.token_balance(&payout_token), funded.milestones[0].amount);
    h.approve(&escrow, 1);
    let stored = h.escrow(&escrow);
    assert_eq!(stored.status, EscrowStatus::Completed);
    assert_eq!(stored.released, TOTAL);
    assert_eq!(h.token_balance(&payout_token), TOTAL);
    assert_eq!(h.token_balance(&vault), 0);
    // No tUSD account took part.
    for owner in [escrow, payout] {
        let tusd_token = get_associated_token_address(&owner, &tusd);
        assert!(h.svm.get_account(&tusd_token).is_none());
    }
}
