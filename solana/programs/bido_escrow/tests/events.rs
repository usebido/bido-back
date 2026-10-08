//! Events (spec 01 section 3.8; T24; ESC-AC-15) and the funded-escrow fixture (section 5).
//!
//! `create_po`, `fund`, `release` and `refund` each emit their event with `emit_cpi!`: a self-CPI
//! into this program whose only account is the event authority, made by the top-level
//! instruction itself. The release of the last pending milestone emits `MilestoneReleased`, then
//! `PoCompleted`, from the same instruction. Every expected event is a struct literal of all its
//! section 3.8 fields, each derived here from the harness's fixture world independently of the
//! program; a test asserts the transaction's whole event stream byte for byte (which events, in
//! which order, from which instruction) and that each event decodes to the expected fields.
//!
//! `tests/fixtures/po_escrow_funded.bin` is the account data of the fixture
//! PO-1042 escrow (`PO_ID`) right after `fund`, for the TypeScript and Python decoders: every key
//! comes from the harness's public seeds and the `Clock` is fixed at `NOW`, so `refund_after` is
//! `NOW + ESCROW_REFUND_AFTER_SECONDS`, and the bytes are the same on every run. With
//! `UPDATE_VECTORS=1` the test writes the file; a plain run fails unless the committed file
//! matches byte for byte.

mod common;

use {
    anchor_lang::{
        prelude::{Clock, Pubkey},
        AccountSerialize, Discriminator, Event, Space,
    },
    bido_escrow::{
        EscrowStatus, Gate, Milestone, MilestoneReleased, MilestoneStatus, PoCompleted, PoCreated,
        PoEscrow, PoFunded, PoRefunded, PO_SEED,
    },
    common::*,
    litesvm::types::TransactionMetadata,
    solana_signer::Signer,
    solana_transaction::versioned::VersionedTransaction,
    std::{fs, path::PathBuf},
};

/// The fixed `Clock` time of every test here: Thu 2026-10-08 10:00 BRT.
const NOW: i64 = 1_791_464_400;
/// Slots at which each step lands, so every event's `slot` is distinct and known.
const CREATE_SLOT: u64 = 100;
const FUND_SLOT: u64 = 200;
const GATE_0_SLOT: u64 = 1_000;
const GATE_1_SLOT: u64 = 2_000;
const REFUND_SLOT: u64 = 5_000;
/// The fixture amounts: 30% / 30% / 40% of $1.2M, i.e. 360,000 / 360,000 / 480,000 tUSD.
const AMOUNTS: [u64; 3] = [360_000_000_000, 360_000_000_000, 480_000_000_000];
/// The funded-escrow fixture, from the repository root.
const FUNDED_FIXTURE: &str = "tests/fixtures/po_escrow_funded.bin";
/// Stack height of a CPI made by a top-level instruction (which is at height 1).
const TOP_LEVEL_CPI: u8 = 2;
/// Top-level instruction indexes: `create_po` after `CB×2` in the intake, `fund` and `refund`
/// alone, and the releases after `[CB×2, CreateAttestation]` in an approval.
const CREATE_PO_IX: usize = 2;
const FUND_IX: usize = 0;
const REFUND_IX: usize = 0;
const FIRST_RELEASE_IX: usize = 3;

/// The fixture world with the `Clock` fixed at `NOW` and `CREATE_SLOT`.
fn harness() -> Harness {
    let mut h = Harness::new();
    let mut clock = h.svm.get_sysvar::<Clock>();
    clock.slot = CREATE_SLOT;
    clock.unix_timestamp = NOW;
    h.svm.set_sysvar(&clock);
    h
}

/// Creates PO-1042 at `CREATE_SLOT`, then funds it at `FUND_SLOT`; returns its escrow.
fn funded_po_1042(h: &mut Harness) -> Pubkey {
    let escrow = h.create_po();
    h.svm.warp_to_slot(FUND_SLOT);
    h.fund(&escrow);
    escrow
}

/// Gate `gate`'s expected SAS attestation address for `escrow`.
fn attestation(h: &Harness, escrow: &Pubkey, gate: u8) -> Pubkey {
    sas_attestation_address(&h.credential, &h.schema, &gate_nonce(escrow, gate))
}

/// The fixture key pinned as gate `gate`'s attester: the buyer for gate 0, the SQE for gate 1.
fn attester(h: &Harness, gate: u8) -> Pubkey {
    [&h.buyer, &h.sqe][usize::from(gate)].pubkey()
}

/// Asserts that `tx` emitted exactly `expected`, in order: each event by self-CPI through the
/// event authority straight from the top-level instruction paired with it, with exactly that
/// event's payload (discriminator, then every field in Borsh).
fn assert_events(
    tx: &VersionedTransaction,
    meta: &TransactionMetadata,
    expected: &[(usize, Vec<u8>)],
) {
    let events = emitted_events(tx, meta);
    for event in &events {
        assert_eq!(
            event.stack_height, TOP_LEVEL_CPI,
            "emitted by the top-level instruction"
        );
    }
    let got: Vec<(usize, Vec<u8>)> = events
        .into_iter()
        .map(|event| (event.instruction, event.payload))
        .collect();
    assert_eq!(got, expected, "events: (top-level instruction, payload)");
}

/// Asserts that the `E` events `tx` emitted decode, with no byte left over, to exactly `expected`,
/// in order: every field of each equals the expected event's.
fn assert_decoded<E: Event>(
    tx: &VersionedTransaction,
    meta: &TransactionMetadata,
    expected: &[&E],
) {
    let got: Vec<Vec<u8>> = emitted::<E>(tx, meta)
        .iter()
        .map(|event| event.data())
        .collect();
    let want: Vec<Vec<u8>> = expected.iter().map(|event| event.data()).collect();
    assert_eq!(got, want, "decoded {} events", std::any::type_name::<E>());
}

// ---- expected events ----

/// The fixture PO's `PoCreated`, created at `CREATE_SLOT` with the `Clock` at `NOW`.
fn expected_po_created(h: &Harness, escrow: &Pubkey) -> PoCreated {
    let payout = h.payout_owner.pubkey();
    PoCreated {
        escrow: *escrow,
        chain_po_id: PO_ID.to_string(),
        buyer: h.buyer.pubkey(),
        payout,
        payout_token: h.ata(&payout),
        mint: h.mint,
        vault: h.ata(escrow),
        po_hash: PO_HASH,
        cad_hash: CAD_HASH,
        total: TOTAL,
        credential: h.credential,
        schema: h.schema,
        gate_attesters: vec![attester(h, 0), attester(h, 1)],
        gate_attestations: vec![attestation(h, escrow, 0), attestation(h, escrow, 1)],
        milestone_names: MILESTONES.iter().map(|m| m.0.to_string()).collect(),
        milestone_bps: MILESTONES.iter().map(|m| m.1).collect(),
        milestone_gates: MILESTONES.iter().map(|m| m.2).collect(),
        milestone_amounts: AMOUNTS.to_vec(),
        refund_after: NOW + ESCROW_REFUND_AFTER_SECONDS,
        slot: CREATE_SLOT,
    }
}

/// The fixture PO's `PoFunded` at `FUND_SLOT`.
fn expected_po_funded(h: &Harness, escrow: &Pubkey) -> PoFunded {
    PoFunded {
        escrow: *escrow,
        chain_po_id: PO_ID.to_string(),
        buyer: h.buyer.pubkey(),
        amount: TOTAL,
        vault: h.ata(escrow),
        slot: FUND_SLOT,
    }
}

/// `MilestoneReleased` of `milestone` in `slot`, released in index order after the ones before
/// it, approved by its gate's attester with `fixture_evidence_hash(gate)`.
fn expected_milestone_released(
    h: &Harness,
    escrow: &Pubkey,
    milestone: u8,
    slot: u64,
) -> MilestoneReleased {
    let m = usize::from(milestone);
    let gate = MILESTONES[m].2;
    let payout = h.payout_owner.pubkey();
    MilestoneReleased {
        escrow: *escrow,
        chain_po_id: PO_ID.to_string(),
        milestone,
        gate,
        amount: AMOUNTS[m],
        released_total: AMOUNTS[..=m].iter().sum(),
        payout,
        payout_token: h.ata(&payout),
        attestation: attestation(h, escrow, gate),
        approved_by: attester(h, gate),
        evidence_hash: fixture_evidence_hash(gate),
        slot,
    }
}

/// The fixture PO's `PoCompleted` in `slot`.
fn expected_po_completed(escrow: &Pubkey, slot: u64) -> PoCompleted {
    PoCompleted {
        escrow: *escrow,
        chain_po_id: PO_ID.to_string(),
        total: TOTAL,
        slot,
    }
}

/// The fixture PO's `PoRefunded` at `REFUND_SLOT`, after Tool start was released: the rest of
/// the total goes back to ATA(buyer, mint).
fn expected_po_refunded(h: &Harness, escrow: &Pubkey) -> PoRefunded {
    PoRefunded {
        escrow: *escrow,
        chain_po_id: PO_ID.to_string(),
        amount: TOTAL - AMOUNTS[0],
        to: h.ata(&h.buyer.pubkey()),
        slot: REFUND_SLOT,
    }
}

/// The fixture PO's escrow right after `fund`: what `PoCreated` announced, `Funded`, nothing
/// released, the milestones pending.
fn expected_funded_escrow(h: &Harness, escrow: &Pubkey) -> PoEscrow {
    let created = expected_po_created(h, escrow);
    let bump = Pubkey::find_program_address(
        &[PO_SEED, created.buyer.as_ref(), PO_ID.as_bytes()],
        &bido_escrow::ID,
    )
    .1;
    let gates = created
        .gate_attesters
        .iter()
        .zip(&created.gate_attestations)
        .map(|(&attester, &attestation)| Gate {
            attester,
            attestation,
        })
        .collect();
    let milestones = MILESTONES
        .iter()
        .zip(AMOUNTS)
        .map(|(&(name, bps, gate), amount)| Milestone {
            name: name.to_string(),
            bps,
            amount,
            gate,
            status: MilestoneStatus::Pending,
            released_slot: 0,
            approved_by: Pubkey::default(),
            evidence_hash: [0; 32],
        })
        .collect();
    PoEscrow {
        chain_po_id: created.chain_po_id,
        buyer: created.buyer,
        payout: created.payout,
        payout_token: created.payout_token,
        mint: created.mint,
        vault: created.vault,
        po_hash: created.po_hash,
        cad_hash: created.cad_hash,
        total: created.total,
        released: 0,
        status: EscrowStatus::Funded,
        credential: created.credential,
        schema: created.schema,
        gates,
        milestones,
        refund_after: created.refund_after,
        bump,
    }
}

// ---- T24 ----

#[test]
fn t24_create_po_emits_po_created() {
    let mut h = harness();
    let accounts = h.create_po_accounts(PO_ID);
    let escrow = accounts.escrow;
    assert_eq!(escrow, escrow_address(&h.buyer.pubkey(), PO_ID));
    let tx = h.create_po_intake_tx(accounts, h.create_po_args(), &PAYOUT_REF);
    let meta = h.send_ok(tx.clone());

    let expected = expected_po_created(&h, &escrow);
    assert_events(&tx, &meta, &[(CREATE_PO_IX, expected.data())]);
    assert_decoded(&tx, &meta, &[&expected]);
}

#[test]
fn t24_fund_emits_po_funded() {
    let mut h = harness();
    let escrow = h.create_po();
    let buyer = h.buyer.pubkey();
    h.mint_to(&buyer, TOTAL);
    h.svm.warp_to_slot(FUND_SLOT);
    let tx = h.fund_tx(h.fund_accounts(&escrow, &buyer), &h.buyer);
    let meta = h.send_ok(tx.clone());

    let expected = expected_po_funded(&h, &escrow);
    assert_events(&tx, &meta, &[(FUND_IX, expected.data())]);
    assert_decoded(&tx, &meta, &[&expected]);
}

#[test]
fn t24_release_emits_milestone_released_and_the_last_one_po_completed() {
    let mut h = harness();
    let escrow = funded_po_1042(&mut h);

    // Gate 0, `[CB×2, CreateAttestation, release(0)]`: Tool start only.
    h.svm.warp_to_slot(GATE_0_SLOT);
    let tx = h.approve_tx(&escrow, 0, &h.buyer, &fixture_evidence_hash(0));
    let meta = h.send_ok(tx.clone());
    let tool_start = expected_milestone_released(&h, &escrow, 0, GATE_0_SLOT);
    assert_events(&tx, &meta, &[(FIRST_RELEASE_IX, tool_start.data())]);
    assert_decoded(&tx, &meta, &[&tool_start]);
    assert_decoded::<PoCompleted>(&tx, &meta, &[]);

    // Gate 1, `[CB×2, CreateAttestation, release(1), release(2)]`: T1, then PPAP, whose release
    // completes the PO and so also emits `PoCompleted`.
    h.svm.warp_to_slot(GATE_1_SLOT);
    let tx = h.approve_tx(&escrow, 1, &h.sqe, &fixture_evidence_hash(1));
    let meta = h.send_ok(tx.clone());
    let t1 = expected_milestone_released(&h, &escrow, 1, GATE_1_SLOT);
    let ppap = expected_milestone_released(&h, &escrow, 2, GATE_1_SLOT);
    assert_eq!(ppap.released_total, TOTAL);
    let completed = expected_po_completed(&escrow, GATE_1_SLOT);
    assert_events(
        &tx,
        &meta,
        &[
            (FIRST_RELEASE_IX, t1.data()),
            (FIRST_RELEASE_IX + 1, ppap.data()),
            (FIRST_RELEASE_IX + 1, completed.data()),
        ],
    );
    assert_decoded(&tx, &meta, &[&t1, &ppap]);
    assert_decoded(&tx, &meta, &[&completed]);
    assert_eq!(h.escrow(&escrow).status, EscrowStatus::Completed);
}

#[test]
fn t24_refund_emits_po_refunded() {
    let mut h = harness();
    let escrow = funded_po_1042(&mut h);
    h.svm.warp_to_slot(GATE_0_SLOT);
    h.approve(&escrow, 0);
    h.warp(ESCROW_REFUND_AFTER_SECONDS);
    assert_eq!(h.now(), h.escrow(&escrow).refund_after);
    h.svm.warp_to_slot(REFUND_SLOT);
    let buyer = h.buyer.pubkey();
    let tx = h.refund_tx(h.refund_accounts(&escrow, &buyer), &h.buyer);
    let meta = h.send_ok(tx.clone());

    let expected = expected_po_refunded(&h, &escrow);
    assert_events(&tx, &meta, &[(REFUND_IX, expected.data())]);
    assert_decoded(&tx, &meta, &[&expected]);
    assert_eq!(h.escrow(&escrow).status, EscrowStatus::Refunded);
}

// ---- the funded-escrow fixture ----

fn funded_fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .join(FUNDED_FIXTURE)
}

#[test]
fn po_escrow_funded_fixture_is_the_funded_po_1042_account() {
    let mut h = harness();
    let escrow = funded_po_1042(&mut h);
    let account = h.svm.get_account(&escrow).expect("escrow account");
    assert_eq!(account.owner, bido_escrow::ID);

    // The account data is the expected funded escrow, then zeros up to the account's size.
    let mut expected = Vec::new();
    expected_funded_escrow(&h, &escrow)
        .try_serialize(&mut expected)
        .expect("serialize PoEscrow");
    expected.resize(PoEscrow::DISCRIMINATOR.len() + PoEscrow::INIT_SPACE, 0);
    assert_eq!(account.data, expected, "funded PO-1042 account data");

    let path = funded_fixture_path();
    if std::env::var("UPDATE_VECTORS").as_deref() == Ok("1") {
        fs::write(&path, &account.data).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    }
    let regenerate = "regenerate with UPDATE_VECTORS=1 cargo test -p bido_escrow --test events";
    let committed =
        fs::read(&path).unwrap_or_else(|e| panic!("{}: {e}; {regenerate}", path.display()));
    assert!(
        committed == account.data,
        "{}: differs from the funded PO-1042 account ({} bytes, expected {}); {regenerate} and \
         commit it",
        path.display(),
        committed.len(),
        account.data.len()
    );
}
