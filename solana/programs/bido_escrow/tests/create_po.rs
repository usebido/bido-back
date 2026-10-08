//! `create_po` (spec 01 section 3.5.1, section 3.4 amounts and gate nonces, section 3.8
//! `PoCreated`; T06, T16, T17; ESC-AC-06, ESC-AC-07): the fixture PO stores its amounts, gates,
//! expected SAS attestation addresses and the configured credential and schema, creates the vault
//! and payout ATAs and emits `PoCreated` through the event authority; a pre-created clean vault is
//! accepted; each of checks 1-14 fails with its own error and no PO, and when two checks fail the
//! earlier one's error wins. Three T17 rows fail before the handler: payout = escrow (Anchor's
//! duplicate mutable account error is accepted), an id of 33 bytes (the escrow PDA derivation
//! aborts) and a duplicate id (the System program's "already in use").

mod common;

use {
    anchor_lang::{
        error::ErrorCode,
        event::EVENT_IX_TAG_LE,
        prelude::{Clock, Pubkey},
        solana_program::{instruction::error::InstructionError, program_option::COption},
        AccountDeserialize, AnchorDeserialize, Discriminator, Space,
    },
    anchor_spl::{
        associated_token::get_associated_token_address,
        token::{self, spl_token::state::Account as TokenAccountState},
    },
    bido_escrow::{
        accounts::CreatePo, CreatePoArgs, EscrowError, EscrowStatus, Gate, Milestone,
        MilestoneInput, MilestoneStatus, PoCreated, PoEscrow, PO_SEED,
    },
    common::*,
    litesvm::types::TransactionMetadata,
    solana_keypair::Keypair,
    solana_signer::Signer,
    solana_transaction::versioned::VersionedTransaction,
};

/// The fixture amounts: 30% / 30% / 40% of $1.2M, i.e. 360,000 / 360,000 / 480,000 tUSD.
const AMOUNTS: [u64; 3] = [360_000_000_000, 360_000_000_000, 480_000_000_000];

/// `SystemError::AccountAlreadyInUse`: what `init` meets on a second `create_po` of the same id.
const ACCOUNT_ALREADY_IN_USE: u32 = 0;

/// `(name, bps, gate)` triples as `create_po` milestone inputs.
fn milestones(spec: &[(&str, u16, u8)]) -> Vec<MilestoneInput> {
    spec.iter()
        .map(|&(name, bps, gate)| MilestoneInput {
            name: name.to_string(),
            bps,
            gate,
        })
        .collect()
}

/// Points the accounts at `mint`: the mint itself, the vault and the payout ATA.
fn with_mint(accounts: &mut CreatePo, mint: Pubkey) {
    accounts.mint = mint;
    accounts.vault = get_associated_token_address(&accounts.escrow, &mint);
    accounts.payout_token = get_associated_token_address(&accounts.payout, &mint);
}

/// Uses `chain_po_id` in the arguments and re-derives the escrow and its vault.
fn with_po_id(accounts: &mut CreatePo, args: &mut CreatePoArgs, chain_po_id: &str) {
    args.chain_po_id = chain_po_id.to_string();
    accounts.escrow = escrow_address(&accounts.buyer, chain_po_id);
    accounts.vault = get_associated_token_address(&accounts.escrow, &accounts.mint);
}

/// Uses `buyer` and re-derives the escrow and its vault for the arguments' id.
fn with_buyer(accounts: &mut CreatePo, args: &CreatePoArgs, buyer: Pubkey) {
    accounts.buyer = buyer;
    accounts.escrow = escrow_address(&buyer, &args.chain_po_id);
    accounts.vault = get_associated_token_address(&accounts.escrow, &accounts.mint);
}

/// Pays out to `payout`: its ATA of the PO's mint becomes the payout token account.
fn with_payout(accounts: &mut CreatePo, payout: Pubkey) {
    accounts.payout = payout;
    accounts.payout_token = get_associated_token_address(&payout, &accounts.mint);
}

/// Creates `escrow`'s vault ahead of `create_po` and gives it a delegate no escrow ever approved.
fn forge_vault_with_delegate(h: &mut Harness, escrow: &Pubkey) -> Pubkey {
    let mint = h.mint;
    let vault = h.create_ata(escrow, &mint);
    let attacker = test_keypair("attacker").pubkey();
    h.edit_token_account(&vault, |state| {
        state.delegate = COption::Some(attacker);
        state.delegated_amount = u64::MAX;
    });
    vault
}

/// A `create_po` transaction signed by whichever fixture key `accounts.operator` is.
fn tx_for(h: &Harness, accounts: CreatePo, args: CreatePoArgs) -> VersionedTransaction {
    let operator: &Keypair = [&h.operator, &h.sqe, &h.buyer]
        .into_iter()
        .find(|key| key.pubkey() == accounts.operator)
        .expect("operator is a fixture key");
    h.tx(&[create_po_ix(accounts, args)], &[operator])
}

/// The one `PoCreated` the transaction emitted by self-CPI through the event authority.
fn po_created(tx: &VersionedTransaction, meta: &TransactionMetadata) -> PoCreated {
    let keys = tx.message.static_account_keys();
    let events: Vec<PoCreated> = meta
        .inner_instructions
        .iter()
        .flatten()
        .map(|inner| &inner.instruction)
        .filter(|ix| keys[usize::from(ix.program_id_index)] == bido_escrow::ID)
        .filter_map(|ix| {
            let body = ix
                .data
                .strip_prefix(EVENT_IX_TAG_LE)?
                .strip_prefix(PoCreated::DISCRIMINATOR)?;
            let signers: Vec<Pubkey> = ix.accounts.iter().map(|&i| keys[usize::from(i)]).collect();
            assert_eq!(signers, [event_authority_address()], "event authority");
            Some(PoCreated::try_from_slice(body).expect("PoCreated"))
        })
        .collect();
    assert_eq!(events.len(), 1, "exactly one PoCreated");
    events.into_iter().next().unwrap()
}

#[test]
fn creates_the_fixture_po() {
    let mut h = Harness::new();
    let args = h.create_po_args();
    let accounts = h.create_po_accounts(PO_ID);
    let (escrow, vault, payout_token) = (accounts.escrow, accounts.vault, accounts.payout_token);
    let payout = h.payout_owner.pubkey();
    let tx = h.create_po_tx(accounts, args.clone());
    h.cu(&tx);
    size(&tx);
    let meta = h.send_ok(tx.clone());
    let slot = h.svm.get_sysvar::<Clock>().slot;

    // The gate nonces and expected attestations, derived independently of the program.
    let attestations: Vec<Pubkey> = (0..2)
        .map(|g| sas_attestation_address(&h.credential, &h.schema, &gate_nonce(&escrow, g)))
        .collect();
    let expected_gates = vec![
        Gate {
            attester: h.buyer.pubkey(),
            attestation: attestations[0],
        },
        Gate {
            attester: h.sqe.pubkey(),
            attestation: attestations[1],
        },
    ];
    let expected_milestones: Vec<Milestone> = MILESTONES
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
    let bump = Pubkey::find_program_address(
        &[PO_SEED, h.buyer.pubkey().as_ref(), PO_ID.as_bytes()],
        &bido_escrow::ID,
    )
    .1;

    let account = h.svm.get_account(&escrow).expect("escrow account");
    assert_eq!(account.owner, bido_escrow::ID);
    assert_eq!(
        account.data.len(),
        PoEscrow::DISCRIMINATOR.len() + PoEscrow::INIT_SPACE
    );
    let stored = h.escrow(&escrow);
    assert_eq!(stored.chain_po_id, PO_ID);
    assert_eq!(stored.buyer, h.buyer.pubkey());
    assert_eq!(stored.payout, payout);
    assert_eq!(stored.payout_token, payout_token);
    assert_eq!(payout_token, h.ata(&payout));
    assert_eq!(stored.mint, h.mint);
    assert_eq!(stored.vault, vault);
    assert_eq!(vault, h.ata(&escrow));
    assert_eq!(stored.po_hash, PO_HASH);
    assert_eq!(stored.cad_hash, CAD_HASH);
    assert_eq!(stored.total, TOTAL);
    assert_eq!(stored.released, 0);
    assert_eq!(stored.status, EscrowStatus::Created);
    assert_eq!(stored.credential, h.credential);
    assert_eq!(stored.schema, h.schema);
    assert_eq!(stored.gates, expected_gates);
    assert_eq!(stored.milestones, expected_milestones);
    assert_eq!(
        stored.milestones.iter().map(|m| m.amount).sum::<u64>(),
        TOTAL
    );
    assert_eq!(stored.refund_after, args.refund_after);
    assert_eq!(stored.bump, bump);

    // Both ATAs exist for the mint: the vault is the escrow's clean, empty token account.
    for (ata, owner) in [(vault, escrow), (payout_token, payout)] {
        let account = h.svm.get_account(&ata).expect("ATA exists");
        assert_eq!(account.owner, token::ID);
        let parsed = token::TokenAccount::try_deserialize(&mut account.data.as_slice())
            .expect("token account");
        assert_eq!(parsed.mint, h.mint);
        assert_eq!(parsed.owner, owner);
        assert_eq!(parsed.amount, 0);
        assert!(parsed.delegate.is_none());
        assert!(parsed.close_authority.is_none());
    }

    let event = po_created(&tx, &meta);
    assert_eq!(event.escrow, escrow);
    assert_eq!(event.chain_po_id, PO_ID);
    assert_eq!(event.buyer, h.buyer.pubkey());
    assert_eq!(event.payout, payout);
    assert_eq!(event.payout_token, payout_token);
    assert_eq!(event.mint, h.mint);
    assert_eq!(event.vault, vault);
    assert_eq!(event.po_hash, PO_HASH);
    assert_eq!(event.cad_hash, CAD_HASH);
    assert_eq!(event.total, TOTAL);
    assert_eq!(event.credential, h.credential);
    assert_eq!(event.schema, h.schema);
    assert_eq!(event.gate_attesters, [h.buyer.pubkey(), h.sqe.pubkey()]);
    assert_eq!(event.gate_attestations, attestations);
    assert_eq!(event.milestone_names, ["Tool start", "T1", "PPAP"]);
    assert_eq!(event.milestone_bps, [3000, 3000, 4000]);
    assert_eq!(event.milestone_gates, [0, 1, 1]);
    assert_eq!(event.milestone_amounts, AMOUNTS);
    assert_eq!(event.refund_after, args.refund_after);
    assert_eq!(event.slot, slot);
}

#[test]
fn t06_milestone_shares_must_add_up_to_10000_bps() {
    let mut h = Harness::new();
    for last_bps in [3999, 4001] {
        let mut args = h.create_po_args();
        args.milestones = milestones(&[
            ("Tool start", 3000, 0),
            ("T1", 3000, 1),
            ("PPAP", last_bps, 1),
        ]);
        let accounts = h.create_po_accounts(PO_ID);
        let escrow = accounts.escrow;
        let tx = h.create_po_tx(accounts, args);
        h.expect_err(tx, EscrowError::BpsSumNot10000);
        assert!(h.svm.get_account(&escrow).is_none(), "no PO may exist");
    }
}

type Edit = Box<dyn Fn(&mut CreatePo, &mut CreatePoArgs)>;

/// Runs each case on the fixture PO and asserts its error and that no escrow was created.
fn assert_cases(h: &mut Harness, cases: Vec<(&str, EscrowError, Edit)>) {
    for (name, error, edit) in cases {
        println!("case: {name}");
        let mut args = h.create_po_args();
        let mut accounts = h.create_po_accounts(PO_ID);
        edit(&mut accounts, &mut args);
        let escrow = accounts.escrow;
        let tx = tx_for(h, accounts, args);
        h.expect_err(tx, error);
        assert!(
            h.svm.get_account(&escrow).is_none(),
            "{name}: no PO may exist"
        );
    }
}

#[test]
fn t17_checks_1_to_9_fail_with_their_errors() {
    let mut h = Harness::new();
    let other_mint = h.new_mint("other-mint");
    let sqe = h.sqe.pubkey();
    let buyer = h.buyer.pubkey();
    let payout = h.payout_owner.pubkey();
    let five_keys: Vec<Pubkey> = ["gate-a", "gate-b", "gate-c", "gate-d", "gate-e"]
        .iter()
        .map(|label| test_keypair(label).pubkey())
        .collect();
    let long_name = "N".repeat(25);

    let cases: Vec<(&str, EscrowError, Edit)> = vec![
        (
            "1: signer is not the configured operator",
            EscrowError::UnauthorizedOperator,
            Box::new(move |accounts, _| accounts.operator = sqe),
        ),
        (
            "2: mint not in allowed_mints",
            EscrowError::MintNotAllowed,
            Box::new(move |accounts, _| with_mint(accounts, other_mint)),
        ),
        (
            "3: id of 5 bytes",
            EscrowError::InvalidPoId,
            Box::new(|accounts, args| with_po_id(accounts, args, "PO-10")),
        ),
        (
            "3: id with a lowercase byte",
            EscrowError::InvalidPoId,
            Box::new(|accounts, args| with_po_id(accounts, args, "PO-1042-7kQ2")),
        ),
        (
            "4: no milestones",
            EscrowError::InvalidMilestoneCount,
            Box::new(|_, args| args.milestones = Vec::new()),
        ),
        (
            "4: five milestones",
            EscrowError::InvalidMilestoneCount,
            Box::new(|_, args| {
                args.milestones = milestones(&[
                    ("A", 2000, 0),
                    ("B", 2000, 1),
                    ("C", 2000, 1),
                    ("D", 2000, 1),
                    ("E", 2000, 1),
                ])
            }),
        ),
        (
            "5: empty name",
            EscrowError::NameTooLong,
            Box::new(|_, args| args.milestones[1].name = String::new()),
        ),
        (
            "5: name of 25 bytes",
            EscrowError::NameTooLong,
            Box::new(move |_, args| args.milestones[2].name = long_name.clone()),
        ),
        (
            "6: zero bps",
            EscrowError::ZeroBps,
            Box::new(|_, args| {
                args.milestones =
                    milestones(&[("Tool start", 0, 0), ("T1", 6000, 1), ("PPAP", 4000, 1)])
            }),
        ),
        (
            "7: total 0",
            EscrowError::ZeroAmount,
            Box::new(|_, args| args.total = 0),
        ),
        (
            "7: an amount that rounds down to 0",
            EscrowError::ZeroAmount,
            Box::new(|_, args| args.total = 3),
        ),
        (
            "8: no gates",
            EscrowError::InvalidGate,
            Box::new(|_, args| args.gate_attesters = Vec::new()),
        ),
        (
            "8: five gates",
            EscrowError::InvalidGate,
            Box::new(move |_, args| args.gate_attesters = five_keys.clone()),
        ),
        (
            "8: gate index out of range",
            EscrowError::InvalidGate,
            Box::new(|_, args| args.milestones[2].gate = 2),
        ),
        (
            "8: unused gate",
            EscrowError::InvalidGate,
            Box::new(|_, args| {
                for milestone in &mut args.milestones {
                    milestone.gate = 0;
                }
            }),
        ),
        (
            "9: default attester",
            EscrowError::InvalidAttester,
            Box::new(move |_, args| args.gate_attesters = vec![Pubkey::default(), sqe]),
        ),
        (
            "9: attester is the payout",
            EscrowError::InvalidAttester,
            Box::new(move |_, args| args.gate_attesters = vec![buyer, payout]),
        ),
    ];
    assert_cases(&mut h, cases);
}

#[test]
fn an_earlier_check_wins() {
    let mut h = Harness::new();
    let other_mint = h.new_mint("other-mint");
    let buyer = h.buyer.pubkey();
    let sqe = h.sqe.pubkey();
    let payout = h.payout_owner.pubkey();
    let stranger = test_keypair("stranger").pubkey();
    let now = h.now();
    let long_name = "N".repeat(25);

    let cases: Vec<(&str, EscrowError, Edit)> = vec![
        (
            "1 before 2: non-operator signer, mint not allowed",
            EscrowError::UnauthorizedOperator,
            Box::new(move |accounts, _| {
                accounts.operator = sqe;
                with_mint(accounts, other_mint);
            }),
        ),
        (
            "2 before 3: mint not allowed, lowercase id",
            EscrowError::MintNotAllowed,
            Box::new(move |accounts, args| {
                with_mint(accounts, other_mint);
                with_po_id(accounts, args, "PO-1042-7kQ2");
            }),
        ),
        (
            "3 before 4: lowercase id, no milestones",
            EscrowError::InvalidPoId,
            Box::new(|accounts, args| {
                with_po_id(accounts, args, "PO-1042-7kQ2");
                args.milestones = Vec::new();
            }),
        ),
        (
            "4 before 5: five milestones with empty names",
            EscrowError::InvalidMilestoneCount,
            Box::new(|_, args| {
                args.milestones = milestones(&[
                    ("", 2000, 0),
                    ("", 2000, 1),
                    ("", 2000, 1),
                    ("", 2000, 1),
                    ("", 2000, 1),
                ])
            }),
        ),
        (
            "5 before 6: name of 25 bytes, zero bps",
            EscrowError::NameTooLong,
            Box::new(move |_, args| {
                args.milestones =
                    milestones(&[("Tool start", 0, 0), ("T1", 6000, 1), ("PPAP", 4000, 1)]);
                args.milestones[0].name = long_name.clone();
            }),
        ),
        (
            "6 before 6: zero bps and a sum of 7,000",
            EscrowError::ZeroBps,
            Box::new(|_, args| args.milestones[0].bps = 0),
        ),
        (
            "6 before 7: sum of 9,999, total 0",
            EscrowError::BpsSumNot10000,
            Box::new(|_, args| {
                args.milestones[2].bps = 3999;
                args.total = 0;
            }),
        ),
        (
            "7 before 8: total 0, unused gate",
            EscrowError::ZeroAmount,
            Box::new(|_, args| {
                args.total = 0;
                args.milestones[1].gate = 0;
                args.milestones[2].gate = 0;
            }),
        ),
        (
            "8 before 9: unused gate, default attester",
            EscrowError::InvalidGate,
            Box::new(move |_, args| {
                args.gate_attesters = vec![Pubkey::default(), sqe];
                for milestone in &mut args.milestones {
                    milestone.gate = 0;
                }
            }),
        ),
        (
            "9 before 10: attester is the payout, which the credential does not list",
            EscrowError::InvalidAttester,
            Box::new(move |_, args| args.gate_attesters = vec![buyer, payout]),
        ),
        (
            "10 before 11: attester not in the credential, payout is the vault",
            EscrowError::GateAttesterMismatch,
            Box::new(move |accounts, args| {
                args.gate_attesters = vec![buyer, stranger];
                let vault = accounts.vault;
                with_payout(accounts, vault);
            }),
        ),
        // 11 and 12 cannot fail together: the buyer would have to be its own escrow or vault.
        (
            "10 before 12: attester not in the credential, payout is the buyer",
            EscrowError::GateAttesterMismatch,
            Box::new(move |accounts, args| {
                args.gate_attesters = vec![buyer, stranger];
                with_buyer(accounts, args, payout);
            }),
        ),
        (
            "11 before 14: payout is the vault, refund_after in the past",
            EscrowError::PayoutIsEscrow,
            Box::new(move |accounts, args| {
                let vault = accounts.vault;
                with_payout(accounts, vault);
                args.refund_after = now - 1;
            }),
        ),
        (
            "12 before 14: payout is the buyer, refund_after in the past",
            EscrowError::PayoutIsBuyer,
            Box::new(move |accounts, args| {
                with_buyer(accounts, args, payout);
                args.refund_after = now - 1;
            }),
        ),
    ];
    assert_cases(&mut h, cases);
}

#[test]
fn an_earlier_check_wins_over_a_vault_with_a_delegate() {
    let mut h = Harness::new();
    let buyer = h.buyer.pubkey();
    let payout = h.payout_owner.pubkey();
    let now = h.now();
    // The vaults of the fixture escrow and of the escrow whose buyer is the payout owner.
    for escrow_buyer in [buyer, payout] {
        forge_vault_with_delegate(&mut h, &escrow_address(&escrow_buyer, PO_ID));
    }

    let cases: Vec<(&str, EscrowError, Edit)> = vec![
        (
            "11 before 13: payout is the vault",
            EscrowError::PayoutIsEscrow,
            Box::new(|accounts, _| {
                let vault = accounts.vault;
                with_payout(accounts, vault);
            }),
        ),
        (
            "12 before 13: payout is the buyer",
            EscrowError::PayoutIsBuyer,
            Box::new(move |accounts, args| with_buyer(accounts, args, payout)),
        ),
        (
            "13 before 14: refund_after in the past",
            EscrowError::VaultAccountInvalid,
            Box::new(move |_, args| args.refund_after = now - 1),
        ),
    ];
    assert_cases(&mut h, cases);
}

#[test]
fn t17_checks_10_to_14_fail_with_their_errors() {
    let mut h = Harness::new();
    let buyer = h.buyer.pubkey();
    let payout = h.payout_owner.pubkey();
    let stranger = test_keypair("stranger").pubkey();
    let now = h.now();

    let cases: Vec<(&str, EscrowError, Edit)> = vec![
        (
            "10: attester not in the credential",
            EscrowError::GateAttesterMismatch,
            Box::new(move |_, args| args.gate_attesters = vec![buyer, stranger]),
        ),
        (
            "11: payout is the vault",
            EscrowError::PayoutIsEscrow,
            Box::new(|accounts, _| {
                let vault = accounts.vault;
                with_payout(accounts, vault);
            }),
        ),
        (
            "12: payout is the buyer",
            EscrowError::PayoutIsBuyer,
            Box::new(move |accounts, args| with_buyer(accounts, args, payout)),
        ),
        (
            "14: refund_after in the past",
            EscrowError::InvalidRefundDeadline,
            Box::new(move |_, args| args.refund_after = now - 1),
        ),
        (
            "14: refund_after is now",
            EscrowError::InvalidRefundDeadline,
            Box::new(move |_, args| args.refund_after = now),
        ),
    ];
    assert_cases(&mut h, cases);
}

#[test]
fn t17_check_10_reads_the_credential_signers_at_creation() {
    let mut h = Harness::new();
    let (credential, buyer, sqe) = (h.credential, h.buyer.pubkey(), h.sqe.pubkey());

    // The SQE (gate 1's attester) is removed from the credential: the fixture PO fails.
    h.change_authorized_signers(&credential, &[buyer]);
    let accounts = h.create_po_accounts(PO_ID);
    let escrow = accounts.escrow;
    let tx = h.create_po_tx(accounts, h.create_po_args());
    h.expect_err(tx, EscrowError::GateAttesterMismatch);
    assert!(h.svm.get_account(&escrow).is_none(), "no PO may exist");

    // Added back, the same PO is created.
    h.change_authorized_signers(&credential, &[buyer, sqe]);
    assert_eq!(h.create_po(), escrow);
}

#[test]
fn t17_payout_equal_to_the_escrow_fails() {
    let mut h = Harness::new();
    let mut accounts = h.create_po_accounts(PO_ID);
    let escrow = accounts.escrow;
    with_payout(&mut accounts, escrow);
    assert_eq!(
        accounts.payout_token, accounts.vault,
        "payout token is the vault"
    );
    let tx = h.create_po_tx(accounts, h.create_po_args());
    h.expect_ix_err(
        tx,
        &[
            InstructionError::Custom(EscrowError::PayoutIsEscrow.into()),
            InstructionError::Custom(ErrorCode::ConstraintDuplicateMutableAccount.into()),
        ],
    );
    assert!(h.svm.get_account(&escrow).is_none(), "no PO may exist");
}

#[test]
fn t17_an_id_of_33_bytes_fails_before_the_handler() {
    let mut h = Harness::new();
    let mut args = h.create_po_args();
    args.chain_po_id = format!("{PO_ID}{}", "X".repeat(33 - PO_ID.len()));
    assert_eq!(args.chain_po_id.len(), 33);
    // No PDA exists for a 33-byte seed: pass the fixture escrow and vault addresses.
    let accounts = h.create_po_accounts(PO_ID);
    let (escrow, vault) = (accounts.escrow, accounts.vault);
    let tx = h.create_po_tx(accounts, args);
    let failed = h.expect_ix_err(tx, &[InstructionError::ProgramFailedToComplete]);
    assert!(
        failed
            .meta
            .logs
            .iter()
            .any(|log| log.contains("Length of the seed is too long")),
        "the escrow PDA derivation aborts\n{}",
        failed.meta.pretty_logs()
    );
    assert!(h.svm.get_account(&escrow).is_none(), "no PO may exist");
    assert!(h.svm.get_account(&vault).is_none(), "no vault may exist");
}

#[test]
fn t17_a_duplicate_id_is_already_in_use() {
    let mut h = Harness::new();
    let escrow = h.create_po();
    let created = h.svm.get_account(&escrow).expect("escrow account");

    // Another PO under the same (buyer, chain_po_id) cannot replace the first.
    let mut args = h.create_po_args();
    args.total = TOTAL / 2;
    let tx = h.create_po_tx(h.create_po_accounts(PO_ID), args);
    h.expect_err(tx, ACCOUNT_ALREADY_IN_USE);
    assert_eq!(h.svm.get_account(&escrow), Some(created), "PO unchanged");
}

#[test]
fn t16_a_pre_created_vault_is_accepted() {
    let mut h = Harness::new();
    let accounts = h.create_po_accounts(PO_ID);
    let (escrow, vault) = (accounts.escrow, accounts.vault);
    let mint = h.mint;
    assert_eq!(h.create_ata(&escrow, &mint), vault);

    let tx = h.create_po_tx(accounts, h.create_po_args());
    h.send_ok(tx);
    let stored = h.escrow(&escrow);
    assert_eq!(stored.vault, vault);
    assert_eq!(stored.status, EscrowStatus::Created);
}

#[test]
fn t16_a_vault_with_a_delegate_or_close_authority_fails() {
    let mut h = Harness::new();
    let attacker = test_keypair("attacker").pubkey();
    let escrow = escrow_address(&h.buyer.pubkey(), PO_ID);
    let mint = h.mint;
    let vault = h.create_ata(&escrow, &mint);

    let forgeries: [(&str, fn(&mut TokenAccountState, Pubkey)); 2] = [
        ("delegate", |state, attacker| {
            state.delegate = COption::Some(attacker);
            state.delegated_amount = u64::MAX;
        }),
        ("close authority", |state, attacker| {
            state.close_authority = COption::Some(attacker);
        }),
    ];
    for (name, forge) in forgeries {
        println!("case: vault with a {name}");
        h.edit_token_account(&vault, |state| {
            clean_vault(state);
            forge(state, attacker);
        });
        let tx = h.create_po_tx(h.create_po_accounts(PO_ID), h.create_po_args());
        h.expect_err(tx, EscrowError::VaultAccountInvalid);
        assert!(
            h.svm.get_account(&escrow).is_none(),
            "{name}: no PO may exist"
        );
    }

    // The same vault, clean again, is accepted.
    h.edit_token_account(&vault, clean_vault);
    assert_eq!(h.create_po(), escrow);
    assert_eq!(h.escrow(&escrow).vault, vault);
    let stored = token::TokenAccount::try_deserialize(
        &mut h.svm.get_account(&vault).expect("vault").data.as_slice(),
    )
    .expect("token account");
    assert!(stored.delegate.is_none() && stored.close_authority.is_none());
}

/// No delegate and no close authority: the vault `create_po` itself would create.
fn clean_vault(state: &mut TokenAccountState) {
    state.delegate = COption::None;
    state.delegated_amount = 0;
    state.close_authority = COption::None;
}
