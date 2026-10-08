//! Golden vectors, Rust side (spec 01 section 3.10, T26; spec 00 section 3.3 `evidence_hash`;
//! ESC-AC-16 Rust part): the top-level `chain` and `codec` objects of
//! `tests/fixtures/vectors.json`, what the TypeScript and Python clients must
//! reproduce for fixed inputs. Addresses are base58, byte strings lowercase hex.
//!
//! `chain` holds addresses and bytes. The inputs are the harness's fixture world, from public
//! seeds: this program's id, the buyer (`test_keypair("buyer")`), `chain_po_id = PO_ID`, the
//! credential `bido-approvals` of `test_keypair("sas-issuer")` and its schema
//! `bido-gate-approval` v1, the tUSD-like mint (`test_keypair("tusd-mint")`), the payout owner
//! (`test_keypair("payout-owner")`), `PO_HASH`, and three evidence files (`EVIDENCE_FILES`) whose
//! sha256 are the fixed file hashes. Every value is derived from the spec's formulas here;
//! `t26_vectors_are_what_the_program_and_sas_accept` proves the program stores the same addresses
//! and SAS and `release` accept the same bytes.
//!
//! `codec` holds what the Python codec (spec 01 section 3.15) must reproduce, all of it written
//! by the program itself in the fixture world with the `Clock` fixed at `NOW` (the world of
//! `events.rs`, so its `Funded` escrow is `po_escrow_funded.bin`): the account data of `Config`
//! and of the PO-1042 `PoEscrow` in each `EscrowStatus`, each with the fields the program decodes
//! from it; the instruction (program id, accounts in order with their flags, data) of `create_po`,
//! `fund`, `release` of each milestone and `refund` for the fixed arguments of `inputs`; the
//! `emit_cpi!` instruction data of each event emitted along the way, with its fields; and the
//! program instructions of each section 3.9 template, by `TxKind`. Every template is executed after
//! `CB×2`: PO-1042 is created, funded and approved gate by gate to `Completed`; in a second world it
//! is refunded after Tool start; in a third, gate 1's attestation alone is cranked by
//! `release_attempt`. `CB×2` is not recorded (its limit and price vary per transaction), and no
//! compiled transaction is either (blockhashes and signatures vary).
//!
//! With `UPDATE_VECTORS=1`, each test replaces its own object (the file is created if absent) and
//! writes every other top-level value back with its text unchanged (specs 02 and 08 write
//! `signing` from Python); a plain run fails if the file or the object is missing or differs.

mod common;

use {
    anchor_lang::{
        event::EVENT_IX_TAG_LE,
        prelude::{Clock, Pubkey},
        solana_program::instruction::Instruction,
        AccountDeserialize, AnchorDeserialize, Discriminator,
    },
    anchor_spl::{associated_token, token},
    bido_escrow::{
        Config, EscrowStatus, MilestoneReleased, MilestoneStatus, PoCompleted, PoCreated, PoEscrow,
        PoFunded, PoRefunded, CONFIG_SEED, GATE_DATA_LEN, PO_SEED, SAS_PROGRAM_ID,
    },
    common::*,
    serde_json::{json, value::RawValue, Map, Value},
    solana_keypair::Keypair,
    solana_signer::Signer,
    std::{
        collections::BTreeMap,
        fs,
        io::ErrorKind,
        path::{Path, PathBuf},
        sync::{Mutex, PoisonError},
    },
};

/// The vectors file, from the repository root.
const VECTORS_FILE: &str = "tests/fixtures/vectors.json";
/// The top-level keys these tests own.
const CHAIN_SECTION: &str = "chain";
const CODEC_SECTION: &str = "codec";
/// Domain of `evidence_hash` (spec 00 section 3.3).
const EVIDENCE_HASH_DOMAIN: &[u8] = b"bido-evidence-v1";
/// Contents of the three fixed evidence files; their sha256 are the vectors' file hashes.
const EVIDENCE_FILES: [&[u8]; 3] = [
    b"bido-vectors:evidence-file-1",
    b"bido-vectors:evidence-file-2",
    b"bido-vectors:evidence-file-3",
];
/// Gates of the fixture PO: gate 0 (buyer approver), gate 1 (SQE).
const GATES: [u8; 2] = [0, 1];
/// Expiry Bido writes into every attestation (spec 01 section 3.6, check 13).
const ATTESTATION_EXPIRY: i64 = 0;

/// The fixed `Clock` time of the `codec` worlds, `events.rs`'s: Thu 2026-10-08 10:00 BRT.
const NOW: i64 = 1_791_464_400;
/// Slots at which each step of the `codec` worlds lands (`events.rs`'s), so every event's `slot`
/// and every milestone's `released_slot` is fixed.
const CREATE_SLOT: u64 = 100;
const FUND_SLOT: u64 = 200;
const GATE_0_SLOT: u64 = 1_000;
const GATE_1_SLOT: u64 = 2_000;
const REFUND_SLOT: u64 = 5_000;

/// Serializes the tests that rewrite the vectors file (cargo runs tests on parallel threads).
static VECTORS_FILE_LOCK: Mutex<()> = Mutex::new(());

/// One `evidence_hash` case: the file hashes in the order a client receives them.
struct EvidenceCase {
    gate: u8,
    file_hashes: Vec<[u8; 32]>,
    evidence_hash: [u8; 32],
}

/// Gate `gate`'s nonce, expected attestation and approval bytes for the fixture PO.
struct GateVector {
    gate: u8,
    nonce: Pubkey,
    attestation: Pubkey,
    evidence_hash: [u8; 32],
    attestation_data: Vec<u8>,
    create_attestation_data: Vec<u8>,
}

struct ChainVectors {
    buyer: Pubkey,
    payout: Pubkey,
    mint: Pubkey,
    credential: Pubkey,
    schema: Pubkey,
    config: (Pubkey, u8),
    escrow: (Pubkey, u8),
    vault: Pubkey,
    payout_token: Pubkey,
    event_authority: Pubkey,
    gates: Vec<GateVector>,
    evidence: Vec<EvidenceCase>,
}

/// `sha256("bido-evidence-v1" ‖ chain_po_id ‖ u8 gate ‖ sorted file hashes)` (spec 00 3.3).
fn evidence_hash(chain_po_id: &str, gate: u8, file_hashes: &[[u8; 32]]) -> [u8; 32] {
    let mut sorted = file_hashes.to_vec();
    sorted.sort_unstable();
    let gate = [gate];
    let mut parts: Vec<&[u8]> = vec![EVIDENCE_HASH_DOMAIN, chain_po_id.as_bytes(), &gate];
    parts.extend(sorted.iter().map(|hash| hash.as_slice()));
    solana_sha256_hasher::hashv(&parts).to_bytes()
}

/// The fixed file hashes in descending byte order, so a client that forgets to sort them fails.
fn file_hashes_descending() -> Vec<[u8; 32]> {
    let mut hashes: Vec<[u8; 32]> = EVIDENCE_FILES
        .iter()
        .map(|file| solana_sha256_hasher::hashv(&[file]).to_bytes())
        .collect();
    hashes.sort_unstable_by(|a, b| b.cmp(a));
    hashes
}

/// Gate approval data (73 bytes): `u32 LE 32 ‖ po_hash ‖ u8 gate ‖ u32 LE 32 ‖ evidence_hash`.
fn attestation_data(po_hash: &[u8; 32], gate: u8, evidence_hash: &[u8; 32]) -> Vec<u8> {
    [
        &32u32.to_le_bytes()[..],
        po_hash,
        &[gate],
        &32u32.to_le_bytes(),
        evidence_hash,
    ]
    .concat()
}

/// SAS `CreateAttestation` data (118 bytes): `0x06 ‖ nonce ‖ u32 LE 73 ‖ data ‖ i64 LE expiry`.
fn create_attestation_data(nonce: &Pubkey, data: &[u8]) -> Vec<u8> {
    [
        &[SAS_IX_CREATE_ATTESTATION][..],
        nonce.as_ref(),
        &(data.len() as u32).to_le_bytes(),
        data,
        &ATTESTATION_EXPIRY.to_le_bytes(),
    ]
    .concat()
}

fn chain_vectors() -> ChainVectors {
    let buyer = test_keypair("buyer").pubkey();
    let payout = test_keypair("payout-owner").pubkey();
    let mint = test_keypair("tusd-mint").pubkey();
    let credential =
        sas_credential_address(&test_keypair("sas-issuer").pubkey(), SAS_CREDENTIAL_NAME);
    let schema = sas_schema_address(&credential, SAS_SCHEMA_NAME, SAS_SCHEMA_VERSION);

    let config = Pubkey::find_program_address(&[CONFIG_SEED], &bido_escrow::ID);
    let escrow = Pubkey::find_program_address(
        &[PO_SEED, buyer.as_ref(), PO_ID.as_bytes()],
        &bido_escrow::ID,
    );
    let event_authority = Pubkey::find_program_address(&[EVENT_AUTHORITY_SEED], &bido_escrow::ID).0;

    let descending = file_hashes_descending();
    let ascending: Vec<[u8; 32]> = descending.iter().rev().copied().collect();
    let evidence = [(0, &descending), (1, &descending), (1, &ascending)]
        .into_iter()
        .map(|(gate, file_hashes)| EvidenceCase {
            gate,
            file_hashes: file_hashes.clone(),
            evidence_hash: evidence_hash(PO_ID, gate, file_hashes),
        })
        .collect();

    let gates = GATES
        .into_iter()
        .map(|gate| {
            let nonce = gate_nonce(&escrow.0, gate);
            let evidence_hash = evidence_hash(PO_ID, gate, &descending);
            let attestation_data = attestation_data(&PO_HASH, gate, &evidence_hash);
            GateVector {
                gate,
                nonce,
                attestation: sas_attestation_address(&credential, &schema, &nonce),
                evidence_hash,
                create_attestation_data: create_attestation_data(&nonce, &attestation_data),
                attestation_data,
            }
        })
        .collect();

    ChainVectors {
        buyer,
        payout,
        mint,
        credential,
        schema,
        config,
        escrow,
        vault: associated_token::get_associated_token_address(&escrow.0, &mint),
        payout_token: associated_token::get_associated_token_address(&payout, &mint),
        event_authority,
        gates,
        evidence,
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

impl ChainVectors {
    fn to_json(&self) -> Value {
        json!({
            "inputs": {
                "program_id": bido_escrow::ID.to_string(),
                "sas_program_id": SAS_PROGRAM_ID.to_string(),
                "token_program": token::ID.to_string(),
                "associated_token_program": associated_token::ID.to_string(),
                "buyer": self.buyer.to_string(),
                "payout": self.payout.to_string(),
                "mint": self.mint.to_string(),
                "credential": self.credential.to_string(),
                "schema": self.schema.to_string(),
                "chain_po_id": PO_ID,
                "po_hash": hex(&PO_HASH),
                "attestation_expiry": ATTESTATION_EXPIRY,
            },
            "config": self.config.0.to_string(),
            "config_bump": self.config.1,
            "escrow": self.escrow.0.to_string(),
            "escrow_bump": self.escrow.1,
            "vault": self.vault.to_string(),
            "payout_token": self.payout_token.to_string(),
            "event_authority": self.event_authority.to_string(),
            "gates": self.gates.iter().map(|g| json!({
                "gate": g.gate,
                "nonce": g.nonce.to_string(),
                "attestation": g.attestation.to_string(),
                "evidence_hash": hex(&g.evidence_hash),
                "attestation_data": hex(&g.attestation_data),
                "create_attestation_data": hex(&g.create_attestation_data),
            })).collect::<Vec<_>>(),
            "evidence_hashes": self.evidence.iter().map(|case| json!({
                "chain_po_id": PO_ID,
                "gate": case.gate,
                "file_hashes": case.file_hashes.iter().map(|h| hex(h)).collect::<Vec<_>>(),
                "evidence_hash": hex(&case.evidence_hash),
            })).collect::<Vec<_>>(),
        })
    }
}

// ---- codec: the program's own accounts, instructions and events ----

/// The fixture world with the `Clock` fixed at `NOW` and `CREATE_SLOT`.
fn codec_harness() -> Harness {
    let mut h = Harness::new();
    let mut clock = h.svm.get_sysvar::<Clock>();
    clock.slot = CREATE_SLOT;
    clock.unix_timestamp = NOW;
    h.svm.set_sysvar(&clock);
    h
}

/// Sends `ixs` after the harness's `CB×2`, paid by the relayer and signed by `signers`; returns
/// each event the transaction emitted, in order, as its vector.
fn execute(h: &mut Harness, ixs: &[Instruction], signers: &[&Keypair]) -> Vec<Value> {
    let mut all = compute_budget_ixs().to_vec();
    all.extend_from_slice(ixs);
    let tx = h.tx(&all, signers);
    let meta = h.send_ok(tx.clone());
    emitted_events(&tx, &meta)
        .iter()
        .map(|event| event_json(&event.payload))
        .collect()
}

/// The account data at `escrow` and the `PoEscrow` the program reads from it.
fn escrow_account(h: &Harness, escrow: &Pubkey) -> (Vec<u8>, PoEscrow) {
    let data = h.svm.get_account(escrow).expect("PoEscrow exists").data;
    let state = PoEscrow::try_deserialize(&mut data.as_slice()).expect("PoEscrow");
    (data, state)
}

/// `release(m)` of every milestone of `escrow`, in index order, against its gate's stored
/// attestation.
fn release_ixs(h: &Harness, escrow: &Pubkey) -> Vec<Instruction> {
    let stored = h.escrow(escrow);
    (0u8..)
        .zip(&stored.milestones)
        .map(|(milestone, m)| {
            let attestation = stored.gates[usize::from(m.gate)].attestation;
            release_ix(h.release_accounts(escrow, &attestation), milestone)
        })
        .collect()
}

/// The `release_attempt` crank of gate `gate` (spec 01 section 3.11 `buildCrankInstructions`):
/// `release(m)` against the gate's stored attestation for each of its `Pending` milestones.
fn crank_ixs(h: &Harness, escrow: &Pubkey, gate: u8) -> Vec<Instruction> {
    let stored = h.escrow(escrow);
    let attestation = stored.gates[usize::from(gate)].attestation;
    (0u8..)
        .zip(&stored.milestones)
        .filter(|(_, m)| m.gate == gate && m.status == MilestoneStatus::Pending)
        .map(|(milestone, _)| release_ix(h.release_accounts(escrow, &attestation), milestone))
        .collect()
}

fn keys_json(keys: &[Pubkey]) -> Vec<String> {
    keys.iter().map(Pubkey::to_string).collect()
}

/// An instruction: program id, accounts in order with their flags, data.
fn instruction_json(ix: &Instruction) -> Value {
    json!({
        "program_id": ix.program_id.to_string(),
        "accounts": ix.accounts.iter().map(|account| json!({
            "pubkey": account.pubkey.to_string(),
            "is_signer": account.is_signer,
            "is_writable": account.is_writable,
        })).collect::<Vec<_>>(),
        "data": hex(&ix.data),
    })
}

fn instructions_json(ixs: &[Instruction]) -> Value {
    Value::Array(ixs.iter().map(instruction_json).collect())
}

fn config_json(config: &Config) -> Value {
    json!({
        "admin": config.admin.to_string(),
        "operator": config.operator.to_string(),
        "allowed_mints": keys_json(&config.allowed_mints),
        "sas_credential": config.sas_credential.to_string(),
        "sas_schema": config.sas_schema.to_string(),
        "bump": config.bump,
    })
}

/// A `PoEscrow`'s fields; enums by variant name.
fn po_escrow_json(escrow: &PoEscrow) -> Value {
    json!({
        "chain_po_id": escrow.chain_po_id,
        "buyer": escrow.buyer.to_string(),
        "payout": escrow.payout.to_string(),
        "payout_token": escrow.payout_token.to_string(),
        "mint": escrow.mint.to_string(),
        "vault": escrow.vault.to_string(),
        "po_hash": hex(&escrow.po_hash),
        "cad_hash": hex(&escrow.cad_hash),
        "total": escrow.total,
        "released": escrow.released,
        "status": format!("{:?}", escrow.status),
        "credential": escrow.credential.to_string(),
        "schema": escrow.schema.to_string(),
        "gates": escrow.gates.iter().map(|gate| json!({
            "attester": gate.attester.to_string(),
            "attestation": gate.attestation.to_string(),
        })).collect::<Vec<_>>(),
        "milestones": escrow.milestones.iter().map(|m| json!({
            "name": m.name,
            "bps": m.bps,
            "amount": m.amount,
            "gate": m.gate,
            "status": format!("{:?}", m.status),
            "released_slot": m.released_slot,
            "approved_by": m.approved_by.to_string(),
            "evidence_hash": hex(&m.evidence_hash),
        })).collect::<Vec<_>>(),
        "refund_after": escrow.refund_after,
        "bump": escrow.bump,
    })
}

/// An account's data and the fields the program decodes from it.
fn account_json(data: &[u8], fields: Value) -> Value {
    json!({ "data": hex(data), "fields": fields })
}

fn po_created_json(e: &PoCreated) -> Value {
    json!({
        "escrow": e.escrow.to_string(),
        "chain_po_id": e.chain_po_id,
        "buyer": e.buyer.to_string(),
        "payout": e.payout.to_string(),
        "payout_token": e.payout_token.to_string(),
        "mint": e.mint.to_string(),
        "vault": e.vault.to_string(),
        "po_hash": hex(&e.po_hash),
        "cad_hash": hex(&e.cad_hash),
        "total": e.total,
        "credential": e.credential.to_string(),
        "schema": e.schema.to_string(),
        "gate_attesters": keys_json(&e.gate_attesters),
        "gate_attestations": keys_json(&e.gate_attestations),
        "milestone_names": e.milestone_names,
        "milestone_bps": e.milestone_bps,
        "milestone_gates": e.milestone_gates,
        "milestone_amounts": e.milestone_amounts,
        "refund_after": e.refund_after,
        "slot": e.slot,
    })
}

fn po_funded_json(e: &PoFunded) -> Value {
    json!({
        "escrow": e.escrow.to_string(),
        "chain_po_id": e.chain_po_id,
        "buyer": e.buyer.to_string(),
        "amount": e.amount,
        "vault": e.vault.to_string(),
        "slot": e.slot,
    })
}

fn milestone_released_json(e: &MilestoneReleased) -> Value {
    json!({
        "escrow": e.escrow.to_string(),
        "chain_po_id": e.chain_po_id,
        "milestone": e.milestone,
        "gate": e.gate,
        "amount": e.amount,
        "released_total": e.released_total,
        "payout": e.payout.to_string(),
        "payout_token": e.payout_token.to_string(),
        "attestation": e.attestation.to_string(),
        "approved_by": e.approved_by.to_string(),
        "evidence_hash": hex(&e.evidence_hash),
        "slot": e.slot,
    })
}

fn po_completed_json(e: &PoCompleted) -> Value {
    json!({
        "escrow": e.escrow.to_string(),
        "chain_po_id": e.chain_po_id,
        "total": e.total,
        "slot": e.slot,
    })
}

fn po_refunded_json(e: &PoRefunded) -> Value {
    json!({
        "escrow": e.escrow.to_string(),
        "chain_po_id": e.chain_po_id,
        "amount": e.amount,
        "to": e.to.to_string(),
        "slot": e.slot,
    })
}

/// `payload` decoded as an `E` (its discriminator, then every field with no byte left over), or
/// `None` if it starts with another discriminator.
fn decode_event<E: AnchorDeserialize + Discriminator>(payload: &[u8]) -> Option<E> {
    let body = payload.strip_prefix(E::DISCRIMINATOR)?;
    Some(E::try_from_slice(body).expect("event fields"))
}

/// An emitted event's vector: its section 3.8 name, the whole `emit_cpi!` instruction data
/// (`EVENT_IX_TAG_LE`, then `payload`: the discriminator and the Borsh fields) and the fields as
/// the program's own type decodes them.
fn event_json(payload: &[u8]) -> Value {
    let (name, fields) = if let Some(e) = decode_event::<PoCreated>(payload) {
        ("PoCreated", po_created_json(&e))
    } else if let Some(e) = decode_event::<PoFunded>(payload) {
        ("PoFunded", po_funded_json(&e))
    } else if let Some(e) = decode_event::<MilestoneReleased>(payload) {
        ("MilestoneReleased", milestone_released_json(&e))
    } else if let Some(e) = decode_event::<PoCompleted>(payload) {
        ("PoCompleted", po_completed_json(&e))
    } else if let Some(e) = decode_event::<PoRefunded>(payload) {
        ("PoRefunded", po_refunded_json(&e))
    } else {
        panic!("not an event of spec 01 section 3.8: {}", hex(payload));
    };
    let mut data = EVENT_IX_TAG_LE.to_vec();
    data.extend_from_slice(payload);
    json!({ "name": name, "data": hex(&data), "fields": fields })
}

fn codec_vectors() -> Value {
    let evidence: Vec<[u8; 32]> = chain_vectors()
        .gates
        .iter()
        .map(|g| g.evidence_hash)
        .collect();
    let operator = test_keypair("operator");
    let buyer = test_keypair("buyer");
    let sqe = test_keypair("sqe");

    // World 1: the templates in order, from intake to `Completed`.
    let mut h = codec_harness();
    let config_data = h.svm.get_account(&config_address()).expect("Config").data;
    let config = Config::try_deserialize(&mut config_data.as_slice()).expect("Config");
    let args = h.create_po_args();
    let accounts = h.create_po_accounts(PO_ID);
    let escrow = accounts.escrow;
    let create_po = create_po_ix(accounts, args.clone());
    let create_po_template = vec![create_po.clone(), payout_ref_memo_ix(&PAYOUT_REF)];
    let mut events = execute(&mut h, &create_po_template, &[&operator]);
    let created = escrow_account(&h, &escrow);

    h.svm.warp_to_slot(FUND_SLOT);
    h.mint_to(&buyer.pubkey(), TOTAL);
    let fund = fund_ix(h.fund_accounts(&escrow, &buyer.pubkey()));
    let fund_template = vec![fund.clone()];
    events.extend(execute(&mut h, &fund_template, &[&buyer]));
    let funded = escrow_account(&h, &escrow);
    let releases = release_ixs(&h, &escrow);
    let release_attempt = crank_ixs(&h, &escrow, 1);

    h.svm.warp_to_slot(GATE_0_SLOT);
    let release_tool_start = h.approval_ixs(&escrow, 0, &buyer.pubkey(), &evidence[0]);
    events.extend(execute(&mut h, &release_tool_start, &[&buyer]));

    h.svm.warp_to_slot(GATE_1_SLOT);
    let approve_ppap = h.approval_ixs(&escrow, 1, &sqe.pubkey(), &evidence[1]);
    events.extend(execute(&mut h, &approve_ppap, &[&sqe]));
    let completed = escrow_account(&h, &escrow);

    // World 2: PO-1042 again, Tool start released, then refunded once the deadline has passed.
    let mut r = codec_harness();
    assert_eq!(r.create_po(), escrow);
    r.svm.warp_to_slot(FUND_SLOT);
    r.fund(&escrow);
    r.svm.warp_to_slot(GATE_0_SLOT);
    let tool_start = r.approval_ixs(&escrow, 0, &buyer.pubkey(), &evidence[0]);
    execute(&mut r, &tool_start, &[&buyer]);
    r.warp(ESCROW_REFUND_AFTER_SECONDS);
    r.svm.warp_to_slot(REFUND_SLOT);
    let refund = refund_ix(r.refund_accounts(&escrow, &buyer.pubkey()));
    events.extend(execute(&mut r, &[refund.clone()], &[&buyer]));
    let refunded = escrow_account(&r, &escrow);

    // World 3: gate 1 attested on its own, then `release_attempt` cranks its two milestones.
    let mut c = codec_harness();
    assert_eq!(c.create_po(), escrow);
    c.svm.warp_to_slot(FUND_SLOT);
    c.fund(&escrow);
    c.svm.warp_to_slot(GATE_1_SLOT);
    let attest = c.attest_tx(&escrow, 1, &sqe, &evidence[1]);
    c.send_ok(attest);
    execute(&mut c, &release_attempt, &[]);
    let cranked = c.escrow(&escrow);

    // Each world reached the state it is recorded for.
    use bido_escrow::{EscrowStatus as E, MilestoneStatus as M};
    let statuses = |e: &PoEscrow| -> (E, Vec<M>) {
        (e.status, e.milestones.iter().map(|m| m.status).collect())
    };
    let pending = vec![M::Pending; 3];
    assert_eq!(statuses(&created.1), (E::Created, pending.clone()));
    assert_eq!(statuses(&funded.1), (E::Funded, pending));
    assert_eq!(statuses(&completed.1), (E::Completed, vec![M::Released; 3]));
    let refund_statuses = vec![M::Released, M::Cancelled, M::Cancelled];
    assert_eq!(statuses(&refunded.1), (E::Refunded, refund_statuses));
    let crank_statuses = vec![M::Pending, M::Released, M::Released];
    assert_eq!(statuses(&cranked), (E::Funded, crank_statuses));
    let names: Vec<&str> = events
        .iter()
        .map(|event| event["name"].as_str().expect("event name"))
        .collect();
    assert_eq!(
        names,
        [
            "PoCreated",
            "PoFunded",
            "MilestoneReleased",
            "MilestoneReleased",
            "MilestoneReleased",
            "PoCompleted",
            "PoRefunded"
        ]
    );

    let mut po_escrow = Map::new();
    for (data, state) in [&created, &funded, &completed, &refunded] {
        let status = format!("{:?}", state.status);
        po_escrow.insert(status, account_json(data, po_escrow_json(state)));
    }
    json!({
        "inputs": {
            "program_id": bido_escrow::ID.to_string(),
            "admin": h.admin.pubkey().to_string(),
            "operator": operator.pubkey().to_string(),
            "relayer": h.relayer.pubkey().to_string(),
            "buyer": buyer.pubkey().to_string(),
            "sqe": sqe.pubkey().to_string(),
            "payout": h.payout_owner.pubkey().to_string(),
            "mint": h.mint.to_string(),
            "credential": h.credential.to_string(),
            "schema": h.schema.to_string(),
            "buyer_token": h.ata(&buyer.pubkey()).to_string(),
            "now": NOW,
            "slots": {
                "create_po": CREATE_SLOT,
                "fund": FUND_SLOT,
                "release_tool_start": GATE_0_SLOT,
                "approve_ppap": GATE_1_SLOT,
                "refund": REFUND_SLOT,
            },
            "create_po": {
                "chain_po_id": args.chain_po_id,
                "po_hash": hex(&args.po_hash),
                "cad_hash": hex(&args.cad_hash),
                "total": args.total,
                "gate_attesters": keys_json(&args.gate_attesters),
                "milestones": args.milestones.iter().map(|m| json!({
                    "name": m.name,
                    "bps": m.bps,
                    "gate": m.gate,
                })).collect::<Vec<_>>(),
                "refund_after": args.refund_after,
            },
            "payout_ref": hex(&PAYOUT_REF),
            "evidence_hashes": evidence.iter().map(|e| hex(e)).collect::<Vec<_>>(),
        },
        "accounts": {
            "config": account_json(&config_data, config_json(&config)),
            "po_escrow": po_escrow,
        },
        "instructions": {
            "create_po": instruction_json(&create_po),
            "fund": instruction_json(&fund),
            "release": instructions_json(&releases),
            "refund": instruction_json(&refund),
        },
        "events": events,
        "templates": {
            "create_po": instructions_json(&create_po_template),
            "fund": instructions_json(&fund_template),
            "release_tool_start": instructions_json(&release_tool_start),
            "approve_ppap": instructions_json(&approve_ppap),
            "release_attempt": instructions_json(&release_attempt),
        },
    })
}

// ---- vectors.json: one top-level section per writer ----

/// The top-level values of a vectors file, each kept as its raw JSON text.
type Sections = BTreeMap<String, Box<RawValue>>;

fn vectors_path() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .join(VECTORS_FILE)
}

fn parse_sections(path: &Path, text: &str) -> Sections {
    serde_json::from_str(text)
        .unwrap_or_else(|e| panic!("{}: not a JSON object: {e}", path.display()))
}

/// Replaces the top-level `key` of the vectors file at `path` with `value` (creating the file if
/// absent) and writes every other top-level value back with its text unchanged.
fn write_section(path: &Path, key: &str, value: &Value) {
    let mut sections = match fs::read_to_string(path) {
        Ok(text) => parse_sections(path, &text),
        Err(e) if e.kind() == ErrorKind::NotFound => Sections::new(),
        Err(e) => panic!("{}: {e}", path.display()),
    };
    // Indented one level, as a top-level value of the pretty-printed file.
    let text = serde_json::to_string_pretty(value)
        .expect("serialize section")
        .replace('\n', "\n  ");
    sections.insert(
        key.to_string(),
        RawValue::from_string(text).expect("section is JSON"),
    );
    let body: Vec<String> = sections
        .iter()
        .map(|(key, value)| format!("  {}: {}", Value::from(key.as_str()), value.get()))
        .collect();
    fs::create_dir_all(path.parent().expect("vectors file has a directory"))
        .expect("create the fixtures directory");
    fs::write(path, format!("{{\n{}\n}}\n", body.join(",\n")))
        .unwrap_or_else(|e| panic!("{}: {e}", path.display()));
}

/// The top-level `key` of the vectors file at `path`; panics if the file or the key is missing.
fn read_section(path: &Path, key: &str) -> Value {
    let regenerate = "regenerate with UPDATE_VECTORS=1 cargo test -p bido_escrow --test vectors";
    let text = fs::read_to_string(path)
        .unwrap_or_else(|e| panic!("{}: {e}; {regenerate}", path.display()));
    let sections = parse_sections(path, &text);
    let raw = sections
        .get(key)
        .unwrap_or_else(|| panic!("{}: no `{key}` object; {regenerate}", path.display()));
    serde_json::from_str(raw.get()).expect("section is JSON")
}

/// With `UPDATE_VECTORS=1`, writes `expected` as the vectors file's `key` object; then asserts
/// that the file's `key` object equals it.
fn check_section(key: &str, expected: &Value) {
    let path = vectors_path();
    let _lock = VECTORS_FILE_LOCK
        .lock()
        .unwrap_or_else(PoisonError::into_inner);
    if std::env::var("UPDATE_VECTORS").as_deref() == Ok("1") {
        write_section(&path, key, expected);
    }
    assert!(
        read_section(&path, key) == *expected,
        "{}: the `{key}` object differs; regenerate with UPDATE_VECTORS=1 and commit it",
        path.display()
    );
}

// ---- tests ----

#[test]
fn t26_chain_section_of_vectors_json() {
    check_section(CHAIN_SECTION, &chain_vectors().to_json());
}

#[test]
fn t26_codec_section_of_vectors_json() {
    check_section(CODEC_SECTION, &codec_vectors());
}

#[test]
fn t26_vectors_are_what_the_program_and_sas_accept() {
    let v = chain_vectors();
    let mut h = Harness::new();

    // The vectors' inputs are the harness's fixture world.
    assert_eq!(h.buyer.pubkey(), v.buyer);
    assert_eq!(h.payout_owner.pubkey(), v.payout);
    assert_eq!(h.mint, v.mint);
    assert_eq!(h.credential, v.credential);
    assert_eq!(h.schema, v.schema);
    assert_eq!(config_address(), v.config.0);
    assert_eq!(h.config().bump, v.config.1);
    assert_eq!(event_authority_address(), v.event_authority);

    // `create_po` stores the same escrow, bump, ATAs and expected attestations.
    let escrow = h.create_po();
    assert_eq!(escrow, v.escrow.0);
    let stored = h.escrow(&escrow);
    assert_eq!(stored.po_hash, PO_HASH);
    assert_eq!(stored.bump, v.escrow.1);
    assert_eq!(stored.vault, v.vault);
    assert_eq!(stored.payout_token, v.payout_token);
    assert_eq!(stored.gates.len(), v.gates.len());
    for g in &v.gates {
        assert_eq!(stored.gates[usize::from(g.gate)].attestation, g.attestation);
    }

    // The vectors' `CreateAttestation` bytes, sent as they are, approve each gate: SAS writes the
    // attestation at the expected address with the vectors' data, and `release` accepts it.
    h.fund(&escrow);
    for g in &v.gates {
        assert_eq!(g.attestation_data.len(), GATE_DATA_LEN);
        assert_eq!(g.create_attestation_data.len(), CREATE_ATTESTATION_DATA_LEN);
        let tx = {
            let signer = h.attester(&escrow, g.gate);
            let mut ixs = compute_budget_ixs().to_vec();
            let built = h.approval_ixs(&escrow, g.gate, &signer.pubkey(), &g.evidence_hash);
            assert_eq!(built[0].data, g.create_attestation_data);
            assert_eq!(built[0].accounts[4].pubkey, g.attestation);
            ixs.push(Instruction {
                data: g.create_attestation_data.clone(),
                ..built[0].clone()
            });
            ixs.extend_from_slice(&built[1..]);
            h.tx(&ixs, &[signer])
        };
        h.send_ok(tx);

        let account = h
            .svm
            .get_account(&g.attestation)
            .expect("attestation exists");
        assert_eq!(account.owner, SAS_PROGRAM_ID);
        assert_eq!(
            &account.data[97..101],
            &(GATE_DATA_LEN as u32).to_le_bytes()
        );
        assert_eq!(&account.data[101..174], g.attestation_data.as_slice());
        let attestation = GateAttestation::decode(&account.data);
        assert_eq!(attestation.nonce, g.nonce);
        assert_eq!(attestation.evidence_hash, g.evidence_hash);
        assert_eq!(attestation.expiry, ATTESTATION_EXPIRY);
    }

    let stored = h.escrow(&escrow);
    assert_eq!(stored.status, EscrowStatus::Completed);
    for m in &stored.milestones {
        assert_eq!(m.status, MilestoneStatus::Released);
        assert_eq!(m.evidence_hash, v.gates[usize::from(m.gate)].evidence_hash);
    }
}

#[test]
fn t26_evidence_hash_sorts_file_hashes_and_binds_the_gate() {
    let v = chain_vectors();
    let [gate_0, gate_1, gate_1_ascending] = v.evidence.as_slice() else {
        panic!("three evidence cases");
    };
    assert!(gate_0.file_hashes.windows(2).all(|w| w[0] > w[1]));
    assert_eq!(gate_1.evidence_hash, gate_1_ascending.evidence_hash);
    assert_ne!(gate_0.evidence_hash, gate_1.evidence_hash);
    for g in &v.gates {
        assert_eq!(
            g.evidence_hash,
            v.evidence[usize::from(g.gate)].evidence_hash
        );
    }
}

#[test]
fn t26_write_section_keeps_every_other_section_unchanged() {
    let path = std::env::temp_dir().join(format!("bido-vectors-{}.json", std::process::id()));
    let signing = "{\n    \"z\": 1.50,\n    \"a\": [1,   2]\n  }";
    let codec = "{\"k\": \"v\"}";
    let original = format!(
        "{{\n  \"signing\": {signing},\n  \"codec\": {codec},\n  \"chain\": {chain}\n}}\n",
        chain = r#"{"old": true}"#,
    );
    fs::write(&path, original).expect("write the temporary vectors file");

    write_section(&path, CHAIN_SECTION, &json!({ "new": [1, 2] }));
    let text = fs::read_to_string(&path).expect("read the temporary vectors file");
    let sections = parse_sections(&path, &text);
    assert_eq!(
        sections.keys().collect::<Vec<_>>(),
        ["chain", "codec", "signing"]
    );
    assert_eq!(sections["signing"].get(), signing);
    assert_eq!(sections["codec"].get(), codec);
    assert_eq!(read_section(&path, CHAIN_SECTION), json!({ "new": [1, 2] }));

    // The codec writer, in turn, keeps the `chain` text it finds as well as `signing`.
    let chain = sections["chain"].get().to_string();
    write_section(&path, CODEC_SECTION, &json!({ "templates": {} }));
    let text = fs::read_to_string(&path).expect("read the temporary vectors file");
    let sections = parse_sections(&path, &text);
    assert_eq!(sections["chain"].get(), chain);
    assert_eq!(sections["signing"].get(), signing);
    assert_eq!(
        read_section(&path, CODEC_SECTION),
        json!({ "templates": {} })
    );

    // An absent file is created with the section alone.
    fs::remove_file(&path).expect("remove the temporary vectors file");
    write_section(&path, CHAIN_SECTION, &json!({ "new": 1 }));
    let text = fs::read_to_string(&path).expect("read the temporary vectors file");
    fs::remove_file(&path).expect("remove the temporary vectors file");
    assert_eq!(text, "{\n  \"chain\": {\n    \"new\": 1\n  }\n}\n");
}
