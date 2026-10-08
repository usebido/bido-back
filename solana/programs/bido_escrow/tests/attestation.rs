//! The attestation checks of `release` (spec 01 section 3.6, checks 5-13; T08-T13; ESC-AC-10): a
//! milestone is released only against the SAS attestation at its gate's address, owned by SAS,
//! well formed, under the escrow's credential and schema, for this escrow, gate and PO document,
//! signed by the gate's pinned attester and unexpired. Another gate's attestation, a forgery under
//! another owner, a SAS-owned forgery with one field wrong, an attestation under an attacker's own
//! credential and schema, an expired attestation and one closed by another authorised signer each
//! fail with their own error and move nothing; the genuine attestation then releases.

mod common;

use {
    anchor_lang::{prelude::Pubkey, solana_program::system_program, AccountSerialize},
    anchor_spl::token,
    bido_escrow::{
        EscrowError, EscrowStatus, MilestoneStatus, PoEscrow, GATE_SCHEMA_LAYOUT,
        SAS_CREDENTIAL_DISC, SAS_PROGRAM_ID, SAS_SCHEMA_DISC,
    },
    common::*,
    solana_account::Account,
    solana_keypair::Keypair,
    solana_signer::Signer,
};

/// A fixed `Clock` time, Sat 2026-10-10 12:00 BRT, for the tests that set an expiry: the harness
/// clock starts at 0, the expiry that means "never expires".
const NOW: i64 = 1_791_644_400;
/// T12's attestation expires one minute after SAS writes it.
const EXPIRY_SECONDS: i64 = 60;
/// Another PO of the same buyer, whose gate-1 nonce T10 forges.
const OTHER_PO_ID: &str = "PO-1043-9WXR";
/// Fixed stand-in for the sha256 of another PO document.
const OTHER_PO_HASH: [u8; 32] = [0x51; 32];
/// Offsets of the three u32 length prefixes in an attestation account (spec 01 section 3.6).
const DATA_LEN_AT: usize = 97;
const PO_HASH_LEN_AT: usize = 101;
const EVIDENCE_HASH_LEN_AT: usize = 138;

/// The account bytes `escrow` serializes to.
fn bytes(escrow: &PoEscrow) -> Vec<u8> {
    let mut out = Vec::new();
    escrow.try_serialize(&mut out).expect("serialize PoEscrow");
    out
}

/// Sets the `Clock` sysvar's `unix_timestamp` to `NOW`.
fn set_clock(h: &mut Harness) {
    h.warp(NOW - h.now());
}

/// The attestation `Harness::attest` has SAS write for gate `gate` of `escrow`: the gate's nonce,
/// the escrow's credential, schema and `po_hash`, `fixture_evidence_hash(gate)`, signed by the
/// gate's pinned attester, never expiring, not tokenized.
fn genuine(h: &Harness, escrow: &Pubkey, gate: u8) -> GateAttestation {
    let stored = h.escrow(escrow);
    GateAttestation {
        nonce: gate_nonce(escrow, gate),
        credential: stored.credential,
        schema: stored.schema,
        po_hash: stored.po_hash,
        gate,
        evidence_hash: fixture_evidence_hash(gate),
        signer: stored.gates[usize::from(gate)].attester,
        expiry: 0,
        token_account: Pubkey::default(),
    }
}

/// Writes a rent-exempt account holding `data`, owned by `owner`, at `address` (`set_account`):
/// a state that no real signer produced.
fn forge(h: &mut Harness, address: &Pubkey, owner: Pubkey, data: Vec<u8>) {
    let lamports = h.svm.minimum_balance_for_rent_exemption(data.len());
    h.svm
        .set_account(
            *address,
            Account {
                lamports,
                data,
                owner,
                executable: false,
                rent_epoch: 0,
            },
        )
        .expect("set_account");
}

/// Deletes the account at `address` (zero lamports).
fn remove(h: &mut Harness, address: &Pubkey) {
    h.svm
        .set_account(*address, Account::default())
        .expect("set_account");
}

/// The encoding of `fields` after one `edit`.
fn with_field(fields: &GateAttestation, edit: impl FnOnce(&mut GateAttestation)) -> Vec<u8> {
    let mut forged = fields.clone();
    edit(&mut forged);
    forged.encode()
}

/// `data` after one raw `edit`, for what `GateAttestation` does not hold: the discriminator, the
/// length prefixes and the account length.
fn with_bytes(data: &[u8], edit: impl FnOnce(&mut Vec<u8>)) -> Vec<u8> {
    let mut forged = data.to_vec();
    edit(&mut forged);
    forged
}

fn put_u32(data: &mut [u8], at: usize, value: u32) {
    data[at..at + 4].copy_from_slice(&value.to_le_bytes());
}

/// `attacker`'s own credential `bido-approvals` (signers `[attacker]`) and the gate schema
/// `bido-gate-approval` v1 under it, created through SAS and paid by the attacker; returns
/// `(credential, schema)`.
fn attacker_world(h: &mut Harness, attacker: &Keypair) -> (Pubkey, Pubkey) {
    let key = attacker.pubkey();
    let credential = sas_credential_address(&key, SAS_CREDENTIAL_NAME);
    let ixs = [
        sas_create_credential_ix(&key, &key, SAS_CREDENTIAL_NAME, &[key]),
        sas_create_schema_ix(
            &key,
            &key,
            &credential,
            SAS_SCHEMA_NAME,
            SAS_SCHEMA_DESCRIPTION,
            &GATE_SCHEMA_LAYOUT,
            &SAS_SCHEMA_FIELDS,
        ),
    ];
    let tx = h.tx(&ixs, &[attacker]);
    h.send_ok(tx);
    let schema = sas_schema_address(&credential, SAS_SCHEMA_NAME, SAS_SCHEMA_VERSION);
    (credential, schema)
}

/// Asserts that the failed releases moved nothing: `escrow` is still `before` byte for byte, its
/// vault holds `total − released` and the payout token exactly `released`.
fn assert_nothing_moved(h: &Harness, escrow: &Pubkey, before: &PoEscrow) {
    assert_eq!(bytes(&h.escrow(escrow)), bytes(before));
    assert_eq!(
        h.token_balance(&before.vault),
        before.total - before.released
    );
    assert_eq!(h.token_balance(&before.payout_token), before.released);
}

#[test]
fn t08_another_gates_attestation_fails_with_attestation_address_mismatch() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    h.fund(&escrow);
    // Both gates' real attestations exist, each signed by its pinned attester; nothing is released.
    let attestation_0 = h.attest(&escrow, 0);
    let attestation_1 = h.attest(&escrow, 1);
    let funded = h.escrow(&escrow);
    for (gate, attestation) in [(0, attestation_0), (1, attestation_1)] {
        let account = h
            .svm
            .get_account(&attestation)
            .expect("gate attestation exists");
        assert_eq!(account.owner, SAS_PROGRAM_ID);
        assert_eq!(
            GateAttestation::decode(&account.data),
            genuine(&h, &escrow, gate)
        );
    }

    // Valid in every field for the escrow's other gate, so at the wrong address for the milestone.
    for (milestone, gate, wrong) in [
        (1, 0, attestation_0),
        (2, 0, attestation_0),
        (0, 1, attestation_1),
    ] {
        println!("case: release({milestone}) with gate {gate}'s attestation");
        let tx = h.release_with(&escrow, milestone, &wrong);
        h.expect_err(tx, EscrowError::AttestationAddressMismatch);
    }
    assert_nothing_moved(&h, &escrow, &funded);

    // Each milestone's own gate attestation releases it.
    for (milestone, attestation) in [(0, attestation_0), (1, attestation_1), (2, attestation_1)] {
        let tx = h.release_with(&escrow, milestone, &attestation);
        h.send_ok(tx);
    }
    assert_eq!(h.escrow(&escrow).status, EscrowStatus::Completed);
    assert_eq!(h.token_balance(&funded.payout_token), TOTAL);
    assert_eq!(h.token_balance(&funded.vault), 0);
}

#[test]
fn t09_a_forgery_owned_by_another_program_fails_with_attestation_missing() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    h.fund(&escrow);
    let funded = h.escrow(&escrow);
    let address = funded.gates[1].attestation;
    assert!(h.svm.get_account(&address).is_none());

    // The bytes the SQE's approval would have SAS write, at gate 1's address, owned by anyone else.
    let data = genuine(&h, &escrow, 1).encode();
    for (case, owner) in [
        ("the System program", system_program::ID),
        ("the escrow program", bido_escrow::ID),
        ("the token program", token::ID),
        (
            "an attacker's program",
            test_keypair("attacker-program").pubkey(),
        ),
    ] {
        forge(&mut h, &address, owner, data.clone());
        for milestone in [1, 2] {
            println!("case: release({milestone}) with the forgery owned by {case}");
            let tx = h.release_with(&escrow, milestone, &address);
            h.expect_err(tx, EscrowError::AttestationMissing);
        }
    }
    assert_nothing_moved(&h, &escrow, &funded);

    // Those were SAS's exact bytes: with the forgery gone, the SQE's real attestation equals them
    // and releases T1.
    remove(&mut h, &address);
    h.attest(&escrow, 1);
    let real = h
        .svm
        .get_account(&address)
        .expect("gate 1 attestation exists");
    assert_eq!(real.owner, SAS_PROGRAM_ID);
    assert_eq!(real.data, data);
    let tx = h.release_with(&escrow, 1, &address);
    h.send_ok(tx);
    assert_eq!(
        h.escrow(&escrow).milestones[1].status,
        MilestoneStatus::Released
    );
}

#[test]
fn t10_a_sas_owned_forgery_with_one_field_wrong_fails_with_that_fields_error() {
    let mut h = Harness::new();
    set_clock(&mut h);
    let escrow = h.create_po();
    h.fund(&escrow);
    let now = h.now();
    let buyer = h.buyer.pubkey();
    let other = escrow_address(&buyer, OTHER_PO_ID);
    let attacker = test_keypair("attacker").pubkey();
    let attacker_credential = sas_credential_address(&attacker, SAS_CREDENTIAL_NAME);
    let attacker_schema =
        sas_schema_address(&attacker_credential, SAS_SCHEMA_NAME, SAS_SCHEMA_VERSION);

    // The SQE's real gate-1 attestation is the reference; each case changes one field of it.
    let address = h.attest(&escrow, 1);
    let funded = h.escrow(&escrow);
    let real = h
        .svm
        .get_account(&address)
        .expect("gate 1 attestation exists");
    let fields = genuine(&h, &escrow, 1);
    assert_eq!(real.owner, SAS_PROGRAM_ID);
    assert_eq!(GateAttestation::decode(&real.data), fields);
    assert_eq!(fields.encode(), real.data);

    let cases = [
        (
            "a credential's discriminator",
            with_bytes(&real.data, |d| d[0] = SAS_CREDENTIAL_DISC),
            EscrowError::AttestationMalformed,
        ),
        (
            "a schema's discriminator",
            with_bytes(&real.data, |d| d[0] = SAS_SCHEMA_DISC),
            EscrowError::AttestationMalformed,
        ),
        (
            "an account one byte short",
            with_bytes(&real.data, |d| {
                d.pop();
            }),
            EscrowError::AttestationMalformed,
        ),
        (
            "an account one byte long",
            with_bytes(&real.data, |d| d.push(0)),
            EscrowError::AttestationMalformed,
        ),
        (
            "a data length of 72",
            with_bytes(&real.data, |d| put_u32(d, DATA_LEN_AT, 72)),
            EscrowError::AttestationMalformed,
        ),
        (
            "a po_hash length of 31",
            with_bytes(&real.data, |d| put_u32(d, PO_HASH_LEN_AT, 31)),
            EscrowError::AttestationMalformed,
        ),
        (
            "an evidence_hash length of 31",
            with_bytes(&real.data, |d| put_u32(d, EVIDENCE_HASH_LEN_AT, 31)),
            EscrowError::AttestationMalformed,
        ),
        (
            "a token account",
            with_field(&fields, |a| a.token_account = attacker),
            EscrowError::AttestationMalformed,
        ),
        (
            "the attacker's credential",
            with_field(&fields, |a| a.credential = attacker_credential),
            EscrowError::AttestationCredentialMismatch,
        ),
        (
            "the attacker's schema",
            with_field(&fields, |a| a.schema = attacker_schema),
            EscrowError::AttestationSchemaMismatch,
        ),
        (
            "gate 0's nonce",
            with_field(&fields, |a| a.nonce = gate_nonce(&escrow, 0)),
            EscrowError::AttestationNonceMismatch,
        ),
        (
            "another PO's gate-1 nonce",
            with_field(&fields, |a| a.nonce = gate_nonce(&other, 1)),
            EscrowError::AttestationNonceMismatch,
        ),
        (
            "another document's po_hash",
            with_field(&fields, |a| a.po_hash = OTHER_PO_HASH),
            EscrowError::AttestationPoHashMismatch,
        ),
        (
            "gate byte 0",
            with_field(&fields, |a| a.gate = 0),
            EscrowError::AttestationGateMismatch,
        ),
        (
            "the buyer as signer",
            with_field(&fields, |a| a.signer = buyer),
            EscrowError::AttesterMismatch,
        ),
        (
            "an expiry one second ago",
            with_field(&fields, |a| a.expiry = now - 1),
            EscrowError::AttestationExpired,
        ),
        (
            "an expiry of now",
            with_field(&fields, |a| a.expiry = now),
            EscrowError::AttestationExpired,
        ),
    ];
    for (case, data, error) in cases {
        println!("case: release(1) with a SAS-owned forgery with {case}");
        forge(&mut h, &address, SAS_PROGRAM_ID, data);
        let tx = h.release_with(&escrow, 1, &address);
        h.expect_err(tx, error);
    }
    assert_nothing_moved(&h, &escrow, &funded);

    // Each case differed from SAS's bytes in that field only: restored, they release T1.
    h.svm.set_account(address, real).expect("set_account");
    let tx = h.release_with(&escrow, 1, &address);
    h.send_ok(tx);
    assert_eq!(
        h.token_balance(&funded.payout_token),
        funded.milestones[1].amount
    );
}

#[test]
fn t11_the_attackers_own_credential_and_schema_fail_with_attestation_address_mismatch() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    h.fund(&escrow);
    let funded = h.escrow(&escrow);
    let attacker = test_keypair("attacker");
    h.svm
        .airdrop(&attacker.pubkey(), ACTOR_LAMPORTS)
        .expect("airdrop");

    // The attacker issues its own credential and gate schema, with itself as the only signer, and
    // attests gate 1 of the escrow with the same data: the gate's nonce, the escrow's `po_hash`,
    // gate 1 and the evidence.
    let (credential, schema) = attacker_world(&mut h, &attacker);
    let ix = sas_create_attestation_ix(
        &attacker.pubkey(),
        &attacker.pubkey(),
        &credential,
        &schema,
        &gate_nonce(&escrow, 1),
        &gate_approval_data(&funded.po_hash, 1, &fixture_evidence_hash(1)),
        0,
    );
    let forged = ix.accounts[4].pubkey;
    let tx = h.tx(&[ix], &[&attacker]);
    h.send_ok(tx);
    let account = h
        .svm
        .get_account(&forged)
        .expect("the attacker's attestation exists");
    assert_eq!(account.owner, SAS_PROGRAM_ID);
    assert_eq!(
        GateAttestation::decode(&account.data),
        GateAttestation {
            credential,
            schema,
            signer: attacker.pubkey(),
            ..genuine(&h, &escrow, 1)
        }
    );
    // Its address derives from the attacker's credential and schema, not the escrow's.
    assert_ne!(forged, funded.gates[1].attestation);

    for milestone in [1, 2] {
        println!("case: release({milestone}) with the attacker's attestation");
        let tx = h.release_with(&escrow, milestone, &forged);
        h.expect_err(tx, EscrowError::AttestationAddressMismatch);
    }
    assert_nothing_moved(&h, &escrow, &funded);
}

#[test]
fn t12_an_expired_attestation_fails_with_attestation_expired() {
    let mut h = Harness::new();
    set_clock(&mut h);
    let escrow = h.create_po();
    h.fund(&escrow);
    let funded = h.escrow(&escrow);
    let address = funded.gates[1].attestation;
    let expiry = h.now() + EXPIRY_SECONDS;

    // The SQE's real gate-1 attestation, written by SAS with an expiry one minute ahead.
    let mut ixs = compute_budget_ixs().to_vec();
    ixs.push(sas_create_attestation_ix(
        &h.relayer.pubkey(),
        &h.sqe.pubkey(),
        &funded.credential,
        &funded.schema,
        &gate_nonce(&escrow, 1),
        &gate_approval_data(&funded.po_hash, 1, &fixture_evidence_hash(1)),
        expiry,
    ));
    let tx = h.tx(&ixs, &[&h.sqe]);
    h.send_ok(tx);
    let account = h
        .svm
        .get_account(&address)
        .expect("gate 1 attestation exists");
    assert_eq!(account.owner, SAS_PROGRAM_ID);
    assert_eq!(
        GateAttestation::decode(&account.data),
        GateAttestation {
            expiry,
            ..genuine(&h, &escrow, 1)
        }
    );

    // Before the expiry it releases T1.
    let tx = h.release_with(&escrow, 1, &address);
    h.send_ok(tx);
    let after_t1 = h.escrow(&escrow);
    assert_eq!(after_t1.milestones[1].status, MilestoneStatus::Released);

    // From the expiry on, PPAP's release fails.
    h.warp(expiry - h.now());
    println!("case: release(2) at the expiry");
    let tx = h.release_with(&escrow, 2, &address);
    h.expect_err(tx, EscrowError::AttestationExpired);
    h.warp(1);
    println!("case: release(2) one second after the expiry");
    let tx = h.release_with(&escrow, 2, &address);
    h.expect_err(tx, EscrowError::AttestationExpired);
    assert_nothing_moved(&h, &escrow, &after_t1);
}

#[test]
fn t13_an_attestation_closed_by_another_authorised_signer_fails_with_attestation_missing() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    h.fund(&escrow);
    let address = h.attest(&escrow, 1);
    let funded = h.escrow(&escrow);
    let buyer = h.buyer.pubkey();

    // The buyer is an authorised signer of the credential but not gate 1's attester, and closes
    // the SQE's attestation through SAS.
    let credential = h
        .svm
        .get_account(&funded.credential)
        .expect("credential exists");
    assert!(SasCredential::decode(&credential.data)
        .authorized_signers
        .contains(&buyer));
    assert_ne!(funded.gates[1].attester, buyer);
    let ix = sas_close_attestation_ix(&h.relayer.pubkey(), &buyer, &funded.credential, &address);
    let tx = h.tx(&[ix], &[&h.buyer]);
    h.send_ok(tx);
    assert!(h.svm.get_account(&address).is_none());

    for milestone in [1, 2] {
        println!("case: release({milestone}) after the close");
        let tx = h.release_with(&escrow, milestone, &address);
        h.expect_err(tx, EscrowError::AttestationMissing);
    }
    assert_nothing_moved(&h, &escrow, &funded);

    // The close only delays the release: the SQE's new approval pays T1 and PPAP.
    h.approve(&escrow, 1);
    let stored = h.escrow(&escrow);
    assert_eq!(stored.milestones[1].status, MilestoneStatus::Released);
    assert_eq!(stored.milestones[2].status, MilestoneStatus::Released);
    assert_eq!(
        h.token_balance(&funded.payout_token),
        funded.milestones[1].amount + funded.milestones[2].amount
    );
}
