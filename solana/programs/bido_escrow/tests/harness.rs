//! Harness and SAS spike (spec 01 section 3.10, T00; ESC-AC-04): the fixture world loads, the
//! hand-built SAS instructions run against the devnet dump, and a real attestation decodes at the
//! offsets of section 3.6.

mod common;

use {
    anchor_lang::{
        error::ErrorCode,
        prelude::Pubkey,
        solana_program::{bpf_loader_upgradeable, instruction::Instruction},
        AccountDeserialize, ProgramData,
    },
    anchor_spl::token,
    bido_escrow::{
        ATTESTATION_ACCOUNT_LEN, GATE_DATA_LEN, GATE_SCHEMA_LAYOUT, SAS_ATTESTATION_DISC,
        SAS_CREDENTIAL_DISC, SAS_PROGRAM_ID, SAS_SCHEMA_DISC,
    },
    common::*,
    solana_signer::Signer,
};

const CHAIN_PO_ID: &str = "PO-1042-7KQ2";
const PO_HASH: [u8; 32] = [0x11; 32];
const EVIDENCE_HASH: [u8; 32] = [0x22; 32];

#[test]
fn t00_sas_spike_attestation_decodes_at_spec_offsets() {
    let mut h = Harness::new();

    let credential = h.svm.get_account(&h.credential).expect("credential exists");
    assert_eq!(credential.owner, SAS_PROGRAM_ID);
    assert_eq!(credential.data[0], SAS_CREDENTIAL_DISC);
    assert_eq!(
        SasCredential::decode(&credential.data),
        SasCredential {
            authority: h.issuer.pubkey(),
            name: SAS_CREDENTIAL_NAME.as_bytes().to_vec(),
            authorized_signers: vec![h.buyer.pubkey(), h.sqe.pubkey()],
        }
    );

    let schema = h.svm.get_account(&h.schema).expect("schema exists");
    assert_eq!(schema.owner, SAS_PROGRAM_ID);
    assert_eq!(schema.data[0], SAS_SCHEMA_DISC);
    assert_eq!(
        &schema.data[1..33],
        h.credential.as_ref(),
        "schema credential at bytes 1..33"
    );
    assert_eq!(
        SasSchema::decode(&schema.data),
        SasSchema {
            credential: h.credential,
            name: SAS_SCHEMA_NAME.as_bytes().to_vec(),
            description: SAS_SCHEMA_DESCRIPTION.as_bytes().to_vec(),
            layout: GATE_SCHEMA_LAYOUT.to_vec(),
            field_names: SAS_SCHEMA_FIELDS.iter().map(|f| f.to_string()).collect(),
            is_paused: false,
            version: SAS_SCHEMA_VERSION,
        }
    );

    // The buyer approves gate 0 of PO-1042 at the address `create_po` will expect.
    let escrow = escrow_address(&h.buyer.pubkey(), CHAIN_PO_ID);
    let nonce = gate_nonce(&escrow, 0);
    let address = sas_attestation_address(&h.credential, &h.schema, &nonce);
    let data = gate_approval_data(&PO_HASH, 0, &EVIDENCE_HASH);
    assert_eq!(data.len(), GATE_DATA_LEN);
    let ix = sas_create_attestation_ix(
        &h.relayer.pubkey(),
        &h.buyer.pubkey(),
        &h.credential,
        &h.schema,
        &nonce,
        &data,
        0,
    );
    assert_eq!(ix.data.len(), CREATE_ATTESTATION_DATA_LEN);
    assert_eq!(ix.accounts[4].pubkey, address);
    let tx = h.tx(&[ix], &[&h.buyer]);
    h.cu(&tx);
    size(&tx);
    h.send_ok(tx);

    let account = h.svm.get_account(&address).expect("attestation exists");
    assert_eq!(account.owner, SAS_PROGRAM_ID);
    let d = &account.data;
    assert_eq!(d.len(), ATTESTATION_ACCOUNT_LEN);
    assert_eq!(d[0], SAS_ATTESTATION_DISC);
    assert_eq!(&d[1..33], nonce.as_ref(), "nonce");
    assert_eq!(&d[33..65], h.credential.as_ref(), "credential");
    assert_eq!(&d[65..97], h.schema.as_ref(), "schema");
    assert_eq!(&d[97..101], &73u32.to_le_bytes(), "data length");
    assert_eq!(&d[101..105], &32u32.to_le_bytes(), "po_hash length");
    assert_eq!(&d[105..137], &PO_HASH, "po_hash");
    assert_eq!(d[137], 0, "gate");
    assert_eq!(&d[138..142], &32u32.to_le_bytes(), "evidence length");
    assert_eq!(&d[142..174], &EVIDENCE_HASH, "evidence_hash");
    assert_eq!(&d[174..206], h.buyer.pubkey().as_ref(), "signer");
    assert_eq!(&d[206..214], &0i64.to_le_bytes(), "expiry");
    assert_eq!(&d[214..246], Pubkey::default().as_ref(), "token_account");

    assert_eq!(
        GateAttestation::decode(d),
        GateAttestation {
            nonce,
            credential: h.credential,
            schema: h.schema,
            po_hash: PO_HASH,
            gate: 0,
            evidence_hash: EVIDENCE_HASH,
            signer: h.buyer.pubkey(),
            expiry: 0,
            token_account: Pubkey::default(),
        }
    );
}

#[test]
fn sas_change_signers_and_close_attestation_run_against_the_dump() {
    let mut h = Harness::new();
    let outsider = test_keypair("outsider");

    // An outsider is not an authorised signer, so SAS refuses its attestation.
    let nonce = gate_nonce(&escrow_address(&h.buyer.pubkey(), CHAIN_PO_ID), 1);
    let data = gate_approval_data(&PO_HASH, 1, &EVIDENCE_HASH);
    let attest = |h: &Harness, signer: &Pubkey| {
        sas_create_attestation_ix(
            &h.relayer.pubkey(),
            signer,
            &h.credential,
            &h.schema,
            &nonce,
            &data,
            0,
        )
    };
    let tx = h.tx(&[attest(&h, &outsider.pubkey())], &[&outsider]);
    assert!(
        h.send(tx).is_err(),
        "an unauthorised signer must not attest"
    );

    // The issuer adds the outsider; the credential now lists three signers.
    let signers = [h.buyer.pubkey(), h.sqe.pubkey(), outsider.pubkey()];
    let ix = sas_change_authorized_signers_ix(
        &h.relayer.pubkey(),
        &h.issuer.pubkey(),
        &h.credential,
        &signers,
    );
    let tx = h.tx(&[ix], &[&h.issuer]);
    h.send_ok(tx);
    let credential = SasCredential::decode(&h.svm.get_account(&h.credential).unwrap().data);
    assert_eq!(credential.authorized_signers, signers.to_vec());

    // The SQE attests gate 1; the outsider, another authorised signer, closes it (T13's shape).
    let address = sas_attestation_address(&h.credential, &h.schema, &nonce);
    let tx = h.tx(&[attest(&h, &h.sqe.pubkey())], &[&h.sqe]);
    h.send_ok(tx);
    assert_eq!(
        GateAttestation::decode(&h.svm.get_account(&address).unwrap().data).signer,
        h.sqe.pubkey()
    );
    let ix = sas_close_attestation_ix(
        &h.relayer.pubkey(),
        &outsider.pubkey(),
        &h.credential,
        &address,
    );
    let tx = h.tx(&[ix], &[&outsider]);
    h.send_ok(tx);
    let closed = h.svm.get_account(&address);
    assert!(
        closed.map_or(true, |a| a.lamports == 0 && a.data.is_empty()),
        "closed attestation must be gone"
    );

    // Back to exactly [buyer, sqe]: the outsider can no longer attest.
    let ix = sas_change_authorized_signers_ix(
        &h.relayer.pubkey(),
        &h.issuer.pubkey(),
        &h.credential,
        &[h.buyer.pubkey(), h.sqe.pubkey()],
    );
    let tx = h.tx(&[ix], &[&h.issuer]);
    h.send_ok(tx);
    let tx = h.tx(&[attest(&h, &outsider.pubkey())], &[&outsider]);
    assert!(h.send(tx).is_err(), "a removed signer must not attest");
}

#[test]
fn sas_event_authority_is_the_documented_pda() {
    let (derived, _) = Pubkey::find_program_address(&[SAS_EVENT_AUTHORITY_SEED], &SAS_PROGRAM_ID);
    assert_eq!(derived, SAS_EVENT_AUTHORITY);
}

#[test]
fn bido_escrow_is_upgradeable_with_the_test_admin_as_authority() {
    let mut h = Harness::new();

    let program = h
        .svm
        .get_account(&bido_escrow::ID)
        .expect("program account");
    assert_eq!(program.owner, bpf_loader_upgradeable::ID);
    assert!(program.executable);
    let program_data_address = bpf_loader_upgradeable::get_program_data_address(&bido_escrow::ID);
    assert_eq!(&program.data[4..36], program_data_address.as_ref());

    let program_data = h
        .svm
        .get_account(&program_data_address)
        .expect("ProgramData account");
    let state = ProgramData::try_deserialize(&mut program_data.data.as_slice())
        .expect("Anchor reads ProgramData");
    assert_eq!(state.upgrade_authority_address, Some(h.admin.pubkey()));

    // The program runs from that deployment: an unknown discriminator reaches Anchor's dispatcher.
    let ix = Instruction {
        program_id: bido_escrow::ID,
        accounts: vec![],
        data: vec![0; 8],
    };
    let tx = h.tx(&[ix], &[]);
    h.expect_err(tx, ErrorCode::InstructionFallbackNotFound);
}

#[test]
fn fixture_world_keys_mint_and_helpers() {
    let mut h = Harness::new();

    // Keys come from documented seeds: stable across runs and pairwise distinct.
    assert_eq!(h.buyer.pubkey(), test_keypair("buyer").pubkey());
    let other = Harness::new();
    assert_eq!(h.actors(), other.actors());
    assert_eq!(
        (h.mint, h.credential, h.schema),
        (other.mint, other.credential, other.schema)
    );
    let mut keys = h.actors();
    keys.push(h.mint);
    keys.sort();
    keys.dedup();
    assert_eq!(keys.len(), 9, "actors and mint are distinct");
    assert_eq!(
        h.credential,
        sas_credential_address(&h.issuer.pubkey(), SAS_CREDENTIAL_NAME)
    );
    assert_eq!(
        h.schema,
        sas_schema_address(&h.credential, SAS_SCHEMA_NAME, SAS_SCHEMA_VERSION)
    );

    // tUSD-like mint: 6 decimals, authority = treasury; mint_to and token_balance agree.
    let mint = h.svm.get_account(&h.mint).expect("mint");
    assert_eq!(mint.owner, token::ID);
    let mint = token::Mint::try_deserialize(&mut mint.data.as_slice()).expect("mint state");
    assert_eq!(mint.decimals, MINT_DECIMALS);
    assert_eq!(mint.mint_authority, Some(h.treasury.pubkey()).into());
    let ata = h.mint_to(&h.buyer.pubkey(), 1_200_000);
    assert_eq!(ata, h.ata(&h.buyer.pubkey()));
    h.mint_to(&h.buyer.pubkey(), 1);
    assert_eq!(h.token_balance(&ata), 1_200_001);
    assert_eq!(h.token_balance(&h.ata(&h.sqe.pubkey())), 0);

    // warp moves the clock.
    let before = h.now();
    h.warp(61);
    assert_eq!(h.now(), before + 61);

    // cu and size report the simulated cost and the wire size of a v0 transaction.
    let stranger = test_keypair("stranger");
    let ix = anchor_lang::solana_program::system_instruction::transfer(
        &h.relayer.pubkey(),
        &stranger.pubkey(),
        1_000_000,
    );
    let tx = h.tx(&[ix], &[]);
    assert!(h.cu(&tx) > 0);
    // Signatures 1 + 64, v0 prefix and header 4, keys 1 + 3 × 32, blockhash 32, one transfer
    // instruction 1 + 17, no lookup table 1.
    assert_eq!(size(&tx), 217);
    h.send_ok(tx);
    assert_eq!(h.svm.get_balance(&stranger.pubkey()), Some(1_000_000));
}
