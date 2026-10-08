//! Config instructions (spec 01 section 3.5 rows `init_config` and `update_config`, T23;
//! ESC-AC-05): only the program upgrade authority configures, the SAS pair must be an unpaused
//! gate schema of its credential, and `update_config` follows a rotated upgrade authority. The
//! `sas` parsers are checked against accounts the real SAS program wrote. `Config` changes never
//! affect an existing escrow (I7): one created and funded before an `update_config` keeps its
//! credential and schema, rejects an approval under the new pair and still releases under its own,
//! changing only the fields of I5.

mod common;

use {
    anchor_lang::{
        error::ErrorCode,
        prelude::{AccountInfo, Pubkey},
        solana_program::bpf_loader_upgradeable,
        AccountSerialize,
    },
    bido_escrow::{
        sas::{self, Credential, Schema},
        Config, EscrowError, EscrowStatus, InitConfigArgs, MilestoneStatus, PoEscrow,
        UpdateConfigArgs, CONFIG_SEED, GATE_SCHEMA_LAYOUT, SAS_PROGRAM_ID,
    },
    common::*,
    solana_account::Account,
    solana_keypair::Keypair,
    solana_signer::Signer,
    solana_transaction::versioned::VersionedTransaction,
};

/// `SystemError::AccountAlreadyInUse`: what `init` meets on a second `init_config`.
const ACCOUNT_ALREADY_IN_USE: u32 = 0;
/// Slot at which gate `g`'s approval lands, so each released milestone's slot is known.
const GATE_SLOTS: [u64; 2] = [1_000, 2_000];

/// Every stored `Config` field, comparable in one assertion.
type ConfigFields = (Pubkey, Pubkey, [Pubkey; 2], Pubkey, Pubkey, u8);

fn fields(config: &Config) -> ConfigFields {
    (
        config.admin,
        config.operator,
        config.allowed_mints,
        config.sas_credential,
        config.sas_schema,
        config.bump,
    )
}

fn no_change() -> UpdateConfigArgs {
    UpdateConfigArgs {
        operator: None,
        allowed_mints: None,
        sas_credential: None,
        sas_schema: None,
    }
}

fn init_tx(
    h: &Harness,
    admin: &Keypair,
    credential: &Pubkey,
    schema: &Pubkey,
) -> VersionedTransaction {
    init_tx_with(h, admin, h.init_config_args(), credential, schema)
}

fn init_tx_with(
    h: &Harness,
    admin: &Keypair,
    args: InitConfigArgs,
    credential: &Pubkey,
    schema: &Pubkey,
) -> VersionedTransaction {
    let ix = init_config_ix(&admin.pubkey(), args, credential, schema);
    h.tx(&[ix], &[admin])
}

fn update_tx(
    h: &Harness,
    admin: &Keypair,
    args: UpdateConfigArgs,
    credential: &Pubkey,
    schema: &Pubkey,
) -> VersionedTransaction {
    let ix = update_config_ix(&admin.pubkey(), args, credential, schema);
    h.tx(&[ix], &[admin])
}

/// The account bytes `escrow` serializes to.
fn bytes(escrow: &PoEscrow) -> Vec<u8> {
    let mut out = Vec::new();
    escrow.try_serialize(&mut out).expect("serialize PoEscrow");
    out
}

fn assert_no_config(h: &Harness) {
    assert!(
        h.svm.get_account(&config_address()).is_none(),
        "no Config may exist"
    );
}

/// The gate schema `bido-gate-approval` v1 of `credential`.
fn gate_schema(h: &mut Harness, credential: &Pubkey) -> Pubkey {
    h.create_schema(
        credential,
        SAS_SCHEMA_NAME,
        &GATE_SCHEMA_LAYOUT,
        &SAS_SCHEMA_FIELDS,
    )
}

/// `sas::parse_credential_signers` on `account` stored at `key`.
fn signers_of(key: &Pubkey, mut account: Account) -> Option<Vec<Pubkey>> {
    let info = AccountInfo::new(
        key,
        false,
        false,
        &mut account.lamports,
        &mut account.data,
        &account.owner,
        false,
    );
    sas::parse_credential_signers(&info)
}

#[test]
fn sas_parsers_read_the_accounts_sas_wrote() {
    let mut h = Harness::without_config();
    let credential = h.svm.get_account(&h.credential).expect("credential");
    let schema = h.svm.get_account(&h.schema).expect("schema");
    assert_eq!(
        (credential.owner, schema.owner),
        (SAS_PROGRAM_ID, SAS_PROGRAM_ID)
    );

    assert_eq!(
        Credential::parse(&credential.data),
        Some(Credential {
            authority: h.issuer.pubkey(),
            authorized_signers: vec![h.buyer.pubkey(), h.sqe.pubkey()],
        })
    );
    assert_eq!(
        Schema::parse(&schema.data),
        Some(Schema {
            credential: h.credential,
            layout: &GATE_SCHEMA_LAYOUT,
            is_paused: false,
            version: SAS_SCHEMA_VERSION,
        })
    );

    // Each parser rejects the other kind, truncated data and nothing at all.
    assert_eq!(Credential::parse(&schema.data), None);
    assert_eq!(Schema::parse(&credential.data), None);
    assert_eq!(
        Credential::parse(&credential.data[..credential.data.len() - 1]),
        None
    );
    assert_eq!(Schema::parse(&schema.data[..schema.data.len() - 1]), None);
    assert_eq!(Credential::parse(&[]), None);
    assert_eq!(Schema::parse(&[]), None);

    // `parse_credential_signers` needs a SAS-owned credential.
    assert_eq!(
        signers_of(&h.credential, credential.clone()),
        Some(vec![h.buyer.pubkey(), h.sqe.pubkey()])
    );
    let foreign = Account {
        owner: test_keypair("attacker").pubkey(),
        ..credential
    };
    assert_eq!(signers_of(&h.credential, foreign), None);
    assert_eq!(signers_of(&h.schema, schema), None);

    // It follows SAS's signer list as the issuer changes it.
    let signers = [h.sqe.pubkey(), h.relayer.pubkey(), h.buyer.pubkey()];
    let credential_key = h.credential;
    h.change_authorized_signers(&credential_key, &signers);
    let credential = h.svm.get_account(&h.credential).expect("credential");
    assert_eq!(
        signers_of(&h.credential, credential),
        Some(signers.to_vec())
    );

    // A schema paused through SAS parses as paused.
    let schema_key = h.schema;
    h.set_schema_paused(&credential_key, &schema_key, true);
    let schema = h.svm.get_account(&h.schema).expect("schema");
    assert!(SasSchema::decode(&schema.data).is_paused);
    assert!(Schema::parse(&schema.data).expect("schema").is_paused);
}

#[test]
fn init_config_stores_the_fixture_config() {
    let h = Harness::new();
    let bump = Pubkey::find_program_address(&[CONFIG_SEED], &bido_escrow::ID).1;
    assert_eq!(
        fields(&h.config()),
        (
            h.admin.pubkey(),
            h.operator.pubkey(),
            [h.mint, Pubkey::default()],
            h.credential,
            h.schema,
            bump,
        )
    );
    let account = h.svm.get_account(&config_address()).expect("Config");
    assert_eq!(account.owner, bido_escrow::ID);
}

#[test]
fn init_config_runs_once() {
    let mut h = Harness::new();
    let before = fields(&h.config());
    let tx = init_tx(&h, &h.admin, &h.credential, &h.schema);
    h.expect_err(tx, ACCOUNT_ALREADY_IN_USE);
    assert_eq!(fields(&h.config()), before);
}

#[test]
fn t23_init_config_by_a_non_upgrade_authority_fails() {
    let mut h = Harness::without_config();

    let tx = init_tx(&h, &h.operator, &h.credential, &h.schema);
    h.expect_err(tx, EscrowError::UnauthorizedAdmin);
    assert_no_config(&h);

    // The ProgramData of another program the signer controls is not this program's.
    let attacker = test_keypair("attacker");
    h.svm
        .airdrop(&attacker.pubkey(), ACTOR_LAMPORTS)
        .expect("airdrop");
    let other_program = test_keypair("attacker-program").pubkey();
    load_upgradeable_program(
        &mut h.svm,
        other_program,
        &program_elf(),
        &attacker.pubkey(),
    );
    let mut ix = init_config_ix(
        &attacker.pubkey(),
        h.init_config_args(),
        &h.credential,
        &h.schema,
    );
    ix.accounts[3].pubkey = bpf_loader_upgradeable::get_program_data_address(&other_program);
    let tx = h.tx(&[ix], &[&attacker]);
    h.expect_err(tx, EscrowError::UnauthorizedAdmin);
    assert_no_config(&h);

    // The upgrade authority itself succeeds.
    let tx = init_tx(&h, &h.admin, &h.credential, &h.schema);
    h.send_ok(tx);
    assert_eq!(h.config().admin, h.admin.pubkey());
}

#[test]
fn t23_init_config_rejects_a_credential_not_owned_by_sas() {
    let mut h = Harness::without_config();
    // The credential's exact bytes, at its address, owned by another program.
    let mut forged = h.svm.get_account(&h.credential).expect("credential");
    forged.owner = test_keypair("attacker-program").pubkey();
    h.svm
        .set_account(h.credential, forged)
        .expect("set_account");

    let tx = init_tx(&h, &h.admin, &h.credential, &h.schema);
    h.expect_err(tx, EscrowError::InvalidSasConfig);
    assert_no_config(&h);
}

#[test]
fn t23_init_config_rejects_a_schema_of_another_credential() {
    let mut h = Harness::without_config();
    let other_credential =
        h.create_credential("bido-approvals-other", &[h.buyer.pubkey(), h.sqe.pubkey()]);
    let other_schema = gate_schema(&mut h, &other_credential);

    let tx = init_tx(&h, &h.admin, &h.credential, &other_schema);
    h.expect_err(tx, EscrowError::InvalidSasConfig);
    assert_no_config(&h);

    // That pair on its own is valid.
    let tx = init_tx(&h, &h.admin, &other_credential, &other_schema);
    h.send_ok(tx);
    assert_eq!(
        (h.config().sas_credential, h.config().sas_schema),
        (other_credential, other_schema)
    );
}

#[test]
fn t23_init_config_rejects_another_schema_layout() {
    let mut h = Harness::without_config();
    let credential = h.credential;
    // `gate` as a U16 instead of a U8.
    let schema = h.create_schema(
        &credential,
        "bido-gate-approval-u16",
        &[13, 1, 13],
        &SAS_SCHEMA_FIELDS,
    );
    assert_eq!(
        SasSchema::decode(&h.svm.get_account(&schema).unwrap().data).layout,
        vec![13, 1, 13]
    );

    let tx = init_tx(&h, &h.admin, &credential, &schema);
    h.expect_err(tx, EscrowError::InvalidSasConfig);
    assert_no_config(&h);
}

#[test]
fn t23_init_config_rejects_a_paused_schema() {
    let mut h = Harness::without_config();
    let (credential, schema) = (h.credential, h.schema);
    h.set_schema_paused(&credential, &schema, true);

    let tx = init_tx(&h, &h.admin, &credential, &schema);
    h.expect_err(tx, EscrowError::InvalidSasConfig);
    assert_no_config(&h);

    // Resumed, the same pair is accepted.
    h.set_schema_paused(&credential, &schema, false);
    let tx = init_tx(&h, &h.admin, &credential, &schema);
    h.send_ok(tx);
}

#[test]
fn init_config_rejects_swapped_sas_accounts() {
    let mut h = Harness::without_config();
    let tx = init_tx(&h, &h.admin, &h.schema, &h.credential);
    h.expect_err(tx, EscrowError::InvalidSasConfig);
    let tx = init_tx(&h, &h.admin, &h.credential, &h.credential);
    h.expect_err(tx, EscrowError::InvalidSasConfig);
    assert_no_config(&h);
}

#[test]
fn init_config_needs_an_operator_and_a_first_mint() {
    let mut h = Harness::without_config();

    let args = InitConfigArgs {
        operator: Pubkey::default(),
        ..h.init_config_args()
    };
    let tx = init_tx_with(&h, &h.admin, args, &h.credential, &h.schema);
    h.expect_err(tx, ErrorCode::RequireKeysNeqViolated);

    let args = InitConfigArgs {
        allowed_mints: [Pubkey::default(), h.mint],
        ..h.init_config_args()
    };
    let tx = init_tx_with(&h, &h.admin, args, &h.credential, &h.schema);
    h.expect_err(tx, ErrorCode::RequireKeysNeqViolated);
    assert_no_config(&h);
}

#[test]
fn t23_update_config_by_a_non_upgrade_authority_fails() {
    let mut h = Harness::new();
    let before = fields(&h.config());

    let args = UpdateConfigArgs {
        operator: Some(h.relayer.pubkey()),
        ..no_change()
    };
    let tx = update_tx(&h, &h.operator, args, &h.credential, &h.schema);
    h.expect_err(tx, EscrowError::UnauthorizedAdmin);
    assert_eq!(fields(&h.config()), before);
}

#[test]
fn t23_update_config_follows_a_rotated_upgrade_authority() {
    let mut h = Harness::new();
    let mut expected = fields(&h.config());
    let new_admin = test_keypair("admin-rotated");

    // `solana program set-upgrade-authority`, through the real upgradeable loader.
    let ix = bpf_loader_upgradeable::set_upgrade_authority_checked(
        &bido_escrow::ID,
        &h.admin.pubkey(),
        &new_admin.pubkey(),
    );
    let tx = h.tx(&[ix], &[&h.admin, &new_admin]);
    h.send_ok(tx);
    assert_eq!(h.upgrade_authority(), Some(new_admin.pubkey()));
    assert_eq!(h.config().admin, h.admin.pubkey(), "not synced yet");

    // The stored admin is no longer the upgrade authority.
    let tx = update_tx(&h, &h.admin, no_change(), &h.credential, &h.schema);
    h.expect_err(tx, EscrowError::UnauthorizedAdmin);

    // The new key succeeds and syncs `admin`; nothing else changes.
    let tx = update_tx(&h, &new_admin, no_change(), &h.credential, &h.schema);
    h.send_ok(tx);
    expected.0 = new_admin.pubkey();
    assert_eq!(fields(&h.config()), expected);
}

#[test]
fn update_config_applies_only_the_some_fields() {
    let mut h = Harness::new();
    let initial = h.config();

    let operator = test_keypair("operator-rotated").pubkey();
    let args = UpdateConfigArgs {
        operator: Some(operator),
        ..no_change()
    };
    let tx = update_tx(&h, &h.admin, args, &h.credential, &h.schema);
    h.send_ok(tx);
    let mut expected = fields(&initial);
    expected.1 = operator;
    assert_eq!(fields(&h.config()), expected);

    let usdc_like = test_keypair("usdc-mint").pubkey();
    let args = UpdateConfigArgs {
        allowed_mints: Some([h.mint, usdc_like]),
        ..no_change()
    };
    let tx = update_tx(&h, &h.admin, args, &h.credential, &h.schema);
    h.send_ok(tx);
    expected.2 = [h.mint, usdc_like];
    assert_eq!(fields(&h.config()), expected);

    // A new credential and schema, passed as the resulting pair.
    let credential = h.create_credential("bido-approvals-v2", &[h.buyer.pubkey(), h.sqe.pubkey()]);
    let schema = gate_schema(&mut h, &credential);
    let args = UpdateConfigArgs {
        sas_credential: Some(credential),
        sas_schema: Some(schema),
        ..no_change()
    };
    let tx = update_tx(&h, &h.admin, args, &credential, &schema);
    h.send_ok(tx);
    expected.3 = credential;
    expected.4 = schema;
    assert_eq!(fields(&h.config()), expected);
}

#[test]
fn update_config_rechecks_the_resulting_sas_pair() {
    let mut h = Harness::new();
    let before = fields(&h.config());
    let (credential, schema) = (h.credential, h.schema);
    let other_credential =
        h.create_credential("bido-approvals-v2", &[h.buyer.pubkey(), h.sqe.pubkey()]);
    let other_schema = gate_schema(&mut h, &other_credential);
    let wrong_layout = h.create_schema(
        &credential,
        "bido-gate-approval-u16",
        &[13, 1, 13],
        &SAS_SCHEMA_FIELDS,
    );

    // Only the credential changes: the stored schema belongs to the old one.
    let args = UpdateConfigArgs {
        sas_credential: Some(other_credential),
        ..no_change()
    };
    let tx = update_tx(&h, &h.admin, args, &other_credential, &schema);
    h.expect_err(tx, EscrowError::InvalidSasConfig);

    // The accounts passed must be the resulting pair, even when it is valid on its own.
    let tx = update_tx(&h, &h.admin, no_change(), &other_credential, &other_schema);
    h.expect_err(tx, EscrowError::InvalidSasConfig);
    let args = UpdateConfigArgs {
        sas_credential: Some(other_credential),
        sas_schema: Some(other_schema),
        ..no_change()
    };
    let tx = update_tx(&h, &h.admin, args, &credential, &schema);
    h.expect_err(tx, EscrowError::InvalidSasConfig);

    // A schema with another layout.
    let args = UpdateConfigArgs {
        sas_schema: Some(wrong_layout),
        ..no_change()
    };
    let tx = update_tx(&h, &h.admin, args, &credential, &wrong_layout);
    h.expect_err(tx, EscrowError::InvalidSasConfig);

    // A credential that is not SAS-owned.
    let forged_key = test_keypair("forged-credential").pubkey();
    let mut forged = h.svm.get_account(&credential).expect("credential");
    forged.owner = test_keypair("attacker-program").pubkey();
    h.svm.set_account(forged_key, forged).expect("set_account");
    let args = UpdateConfigArgs {
        sas_credential: Some(forged_key),
        ..no_change()
    };
    let tx = update_tx(&h, &h.admin, args, &forged_key, &schema);
    h.expect_err(tx, EscrowError::InvalidSasConfig);

    // The stored pair, once its schema is paused, fails even with no field changed.
    h.set_schema_paused(&credential, &schema, true);
    let tx = update_tx(&h, &h.admin, no_change(), &credential, &schema);
    h.expect_err(tx, EscrowError::InvalidSasConfig);

    assert_eq!(fields(&h.config()), before);
}

#[test]
fn t23_an_escrow_created_before_update_config_still_releases_with_its_original_pair() {
    let mut h = Harness::new();
    let (credential, schema) = (h.credential, h.schema);
    let escrow = h.create_po();
    h.fund(&escrow);
    let funded = h.escrow(&escrow);
    assert_eq!(funded.status, EscrowStatus::Funded);
    assert_eq!((funded.credential, funded.schema), (credential, schema));
    let mut expected_config = fields(&h.config());

    // Every `Config` field changes: a new credential (same signers) and gate schema, another
    // operator, and a mint list without the escrow's mint.
    let new_credential =
        h.create_credential("bido-approvals-v2", &[h.buyer.pubkey(), h.sqe.pubkey()]);
    let new_schema = gate_schema(&mut h, &new_credential);
    let new_operator = test_keypair("operator-rotated").pubkey();
    let new_mint = test_keypair("usdc-mint").pubkey();
    let args = UpdateConfigArgs {
        operator: Some(new_operator),
        allowed_mints: Some([new_mint, Pubkey::default()]),
        sas_credential: Some(new_credential),
        sas_schema: Some(new_schema),
    };
    let tx = update_tx(&h, &h.admin, args, &new_credential, &new_schema);
    h.send_ok(tx);
    expected_config.1 = new_operator;
    expected_config.2 = [new_mint, Pubkey::default()];
    expected_config.3 = new_credential;
    expected_config.4 = new_schema;
    assert_eq!(fields(&h.config()), expected_config);
    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&funded));

    // Gate 0's approval under the new pair, by the gate's attester, lands at another address.
    let nonce = gate_nonce(&escrow, 0);
    let attestation_ix = sas_create_attestation_ix(
        &h.relayer.pubkey(),
        &h.buyer.pubkey(),
        &new_credential,
        &new_schema,
        &nonce,
        &gate_approval_data(&funded.po_hash, 0, &fixture_evidence_hash(0)),
        0,
    );
    let new_pair_attestation = attestation_ix.accounts[4].pubkey;
    assert_eq!(
        new_pair_attestation,
        sas_attestation_address(&new_credential, &new_schema, &nonce)
    );
    assert_ne!(new_pair_attestation, funded.gates[0].attestation);
    let mut ixs = compute_budget_ixs().to_vec();
    ixs.push(attestation_ix);
    ixs.push(release_ix(
        h.release_accounts(&escrow, &new_pair_attestation),
        0,
    ));
    let tx = h.tx(&ixs, &[&h.buyer]);
    h.expect_err(tx, EscrowError::AttestationAddressMismatch);
    assert!(h.svm.get_account(&new_pair_attestation).is_none());
    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&funded));

    // Both gates approve under the escrow's own pair and release the whole total.
    for gate in 0..2u8 {
        h.svm.warp_to_slot(GATE_SLOTS[usize::from(gate)]);
        h.approve(&escrow, gate);
        let attestation = h
            .svm
            .get_account(&funded.gates[usize::from(gate)].attestation)
            .expect("gate attestation exists");
        assert_eq!(attestation.owner, SAS_PROGRAM_ID);
        let decoded = GateAttestation::decode(&attestation.data);
        assert_eq!((decoded.credential, decoded.schema), (credential, schema));
    }
    assert_eq!(h.token_balance(&funded.payout_token), TOTAL);
    assert_eq!(h.token_balance(&funded.vault), 0);

    // Only the fields of I5 changed; credential, schema, mint and the rest are as created.
    let mut expected = funded.clone();
    expected.status = EscrowStatus::Completed;
    expected.released = TOTAL;
    for m in &mut expected.milestones {
        let gate = usize::from(m.gate);
        m.status = MilestoneStatus::Released;
        m.released_slot = GATE_SLOTS[gate];
        m.approved_by = funded.gates[gate].attester;
        m.evidence_hash = fixture_evidence_hash(m.gate);
    }
    assert_eq!(bytes(&h.escrow(&escrow)), bytes(&expected));
}
