//! LiteSVM harness shared by every `bido_escrow` integration test (spec 01 section 3.10).
//!
//! `Harness::new()` builds the fixture world: `bido_escrow` loaded as an upgradeable program whose
//! upgrade authority is the test admin, the SAS program dumped from devnet at `SAS_PROGRAM_ID`, a
//! tUSD-like 6-decimal mint, the actors (buyer = buyer approver, SQE, relayer, operator, payout
//! owner) and the SAS credential `bido-approvals` (signers `[buyer, sqe]`) with the schema
//! `bido-gate-approval` v1 (layout `[13, 0, 13]`), created through the real SAS program, and the
//! `Config` that `init_config` writes for them (`Harness::without_config()` stops before it).
//! `create_po()` adds the fixture PO: `TOTAL`, the demo milestones Tool start / T1 / PPAP
//! (3000/3000/4000 bps, gates `[0, 1, 1]`) and gates `[buyer, sqe]` (`create_po_with_id()` under
//! another id); `fund()` mints a PO's total to the buyer and funds it; `approve()` sends a gate's
//! approval in the layout of section 3.9 (`[CB×2, CreateAttestation, release…]`), `attest()` and
//! `attest_tx()` only its attestation, and `release_with()` builds a `release_attempt`;
//! `refund_tx()` builds a buyer's `refund`; `create_po_intake_tx()` builds the `create_po` intake
//! (`[CB×2, create_po, Memo]`). `GateAttestation` decodes an attestation account at the offsets
//! of section 3.6 and encodes one back, for forgeries. `emitted_events()` lists the events a
//! transaction emitted through the event authority (section 3.8), `emitted()` decodes one kind.
//!
//! Every key comes from `test_keypair(label)`, a public seed, so no test key can ever hold value.
//! Helpers that call this program's instructions are added by the tasks that implement them.

// Each test file compiles this module on its own and uses only part of it.
#![allow(dead_code)]

use {
    anchor_lang::{
        event::EVENT_IX_TAG_LE,
        prelude::{pubkey, Clock, Pubkey},
        solana_program::{
            bpf_loader_upgradeable,
            instruction::{error::InstructionError, AccountMeta, Instruction},
            program_pack::Pack,
            system_instruction, system_program,
        },
        AccountDeserialize, AnchorDeserialize, Discriminator, InstructionData, ProgramData,
        ToAccountMetas,
    },
    anchor_spl::{associated_token, token},
    bido_escrow::{
        Config, CreatePoArgs, InitConfigArgs, MilestoneInput, MilestoneStatus, PoEscrow,
        UpdateConfigArgs, CONFIG_SEED, GATE_DATA_LEN, GATE_NONCE_DOMAIN, GATE_SCHEMA_LAYOUT,
        PO_SEED, SAS_PROGRAM_ID,
    },
    litesvm::{
        types::{FailedTransactionMetadata, TransactionMetadata, TransactionResult},
        LiteSVM,
    },
    solana_account::Account,
    solana_keypair::Keypair,
    solana_message::{v0, VersionedMessage},
    solana_signer::Signer,
    solana_transaction::versioned::VersionedTransaction,
    solana_transaction_error::TransactionError,
};

/// Domain of the public test-key seeds: `sha256(TEST_KEY_DOMAIN ‖ label)`.
pub const TEST_KEY_DOMAIN: &[u8] = b"bido-escrow-test:";

/// SAS event authority, PDA `["__event_authority"]` of the SAS program (`CloseAttestation`).
pub const SAS_EVENT_AUTHORITY: Pubkey = pubkey!("DzSpKpST2TSyrxokMXchFz3G2yn5WEGoxzpGEUDjCX4g");
pub const SAS_CREDENTIAL_SEED: &[u8] = b"credential";
pub const SAS_SCHEMA_SEED: &[u8] = b"schema";
pub const SAS_ATTESTATION_SEED: &[u8] = b"attestation";
pub const SAS_EVENT_AUTHORITY_SEED: &[u8] = b"__event_authority";
/// Seed of this program's `#[event_cpi]` event authority PDA (Anchor's, the same string as SAS's).
pub const EVENT_AUTHORITY_SEED: &[u8] = b"__event_authority";

/// SAS instruction tags (one leading byte).
pub const SAS_IX_CREATE_CREDENTIAL: u8 = 0;
pub const SAS_IX_CREATE_SCHEMA: u8 = 1;
pub const SAS_IX_CHANGE_SCHEMA_STATUS: u8 = 2;
pub const SAS_IX_CHANGE_AUTHORIZED_SIGNERS: u8 = 3;
pub const SAS_IX_CREATE_ATTESTATION: u8 = 6;
pub const SAS_IX_CLOSE_ATTESTATION: u8 = 7;

/// The credential and schema of spec 01 section 3.12, as the devnet setup creates them.
pub const SAS_CREDENTIAL_NAME: &str = "bido-approvals";
pub const SAS_SCHEMA_NAME: &str = "bido-gate-approval";
pub const SAS_SCHEMA_VERSION: u8 = 1;
pub const SAS_SCHEMA_DESCRIPTION: &str =
    "Approval of a PO gate; releases escrowed funds to the fixed payout";
pub const SAS_SCHEMA_FIELDS: [&str; 3] = ["po_hash", "gate", "evidence_hash"];

/// `CreateAttestation` data: tag, nonce, u32 data length, gate data, i64 expiry (spec 01 3.9).
pub const CREATE_ATTESTATION_DATA_LEN: usize = 1 + 32 + 4 + GATE_DATA_LEN + 8; // 118

/// Compute budget program; its `SetComputeUnitLimit` (tag 2, u32 LE) and `SetComputeUnitPrice`
/// (tag 3, u64 LE) open every Bido transaction (`CB×2`, spec 01 section 3.9).
pub const COMPUTE_BUDGET_PROGRAM_ID: Pubkey =
    pubkey!("ComputeBudget111111111111111111111111111111");
pub const CB_IX_SET_COMPUTE_UNIT_LIMIT: u8 = 2;
pub const CB_IX_SET_COMPUTE_UNIT_PRICE: u8 = 3;
/// The harness's compute unit limit: the maximum, so CU is measured, not capped (spec 01 3.10).
pub const HARNESS_CU_LIMIT: u32 = 1_400_000;
/// The harness's compute unit price: spec 02's `CU_PRICE_FLOOR`, in micro-lamports.
pub const HARNESS_CU_PRICE: u64 = 1_000;

/// SPL Memo (spec 08 section 3.5), one of the programs `LiteSVM::new()` loads; the `create_po`
/// intake transaction carries the PO's `payout_ref` in it (spec 01 section 3.9).
pub const MEMO_PROGRAM_ID: Pubkey = pubkey!("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr");
/// The memo is this prefix, then the `payout_ref` as 64 lowercase hex (spec 08 section 3.2).
pub const PAYOUT_REF_MEMO_PREFIX: &str = "bido:payout_ref:";
/// Largest serialized transaction the cluster accepts, in bytes (spec 01 section 3.9).
pub const PACKET_DATA_SIZE: usize = 1_232;

/// tUSD-like test dollar.
pub const MINT_DECIMALS: u8 = 6;
pub const SPL_MINT_LEN: usize = 82;
/// Lamports airdropped to every actor.
pub const ACTOR_LAMPORTS: u64 = 10_000_000_000;

/// The fixture PO (spec 01 section 3.10, spec 00 section 3.3): PO-1042, $1.2M in base units.
pub const PO_ID: &str = "PO-1042-7KQ2";
pub const TOTAL: u64 = 1_200_000_000_000;
/// `(name, bps, gate)` of the demo milestones.
pub const MILESTONES: [(&str, u16, u8); 3] =
    [("Tool start", 3000, 0), ("T1", 3000, 1), ("PPAP", 4000, 1)];
/// Fixed stand-ins for sha256(PO PDF) and sha256(drawing 2D PDF rev C).
pub const PO_HASH: [u8; 32] = [0x50; 32];
pub const CAD_HASH: [u8; 32] = [0xCA; 32];
/// Fixed stand-in for the fixture PO's `payout_ref` (spec 00 section 3.3).
pub const PAYOUT_REF: [u8; 32] = [0x9E; 32];
/// The `create_po` builder's refund deadline offset: 90 days (spec 01 section 3.5.1).
pub const ESCROW_REFUND_AFTER_SECONDS: i64 = 7_776_000;

/// Fixed stand-in for the `evidence_hash` a gate's fixture approval attests.
pub fn fixture_evidence_hash(gate: u8) -> [u8; 32] {
    [0xE0 | gate; 32]
}

/// Throwaway key from a public seed: `Keypair::new_from_array(sha256(TEST_KEY_DOMAIN ‖ label))`.
pub fn test_keypair(label: &str) -> Keypair {
    let seed = solana_sha256_hasher::hashv(&[TEST_KEY_DOMAIN, label.as_bytes()]);
    Keypair::new_from_array(seed.to_bytes())
}

pub struct Harness {
    pub svm: LiteSVM,
    /// Program upgrade authority (seed label `admin`).
    pub admin: Keypair,
    /// SAS credential authority (`sas-issuer`).
    pub issuer: Keypair,
    /// tUSD mint authority (`treasury`).
    pub treasury: Keypair,
    /// Signs `create_po` (`operator`).
    pub operator: Keypair,
    /// Fee payer of every transaction and rent payer (`relayer`).
    pub relayer: Keypair,
    /// Buyer and buyer approver: gate 0's attester and an authorised SAS signer (`buyer`).
    pub buyer: Keypair,
    /// Supplier quality engineer: gate 1's attester and an authorised SAS signer (`sqe`).
    pub sqe: Keypair,
    /// The factory's payout owner wallet (`payout-owner`).
    pub payout_owner: Keypair,
    /// tUSD-like mint (`tusd-mint`), `MINT_DECIMALS`, authority `treasury`.
    pub mint: Pubkey,
    /// SAS credential `bido-approvals` of `issuer`, signers `[buyer, sqe]`.
    pub credential: Pubkey,
    /// SAS schema `bido-gate-approval` v1 under `credential`.
    pub schema: Pubkey,
}

impl Harness {
    /// The fixture world, `Config` included.
    pub fn new() -> Self {
        let mut h = Self::without_config();
        h.init_config();
        h
    }

    /// The fixture world before `init_config`: programs, actors, mint, credential and schema.
    pub fn without_config() -> Self {
        let dir = env!("CARGO_MANIFEST_DIR");
        let mut svm = LiteSVM::new();
        let admin = test_keypair("admin");

        load_upgradeable_program(&mut svm, bido_escrow::ID, &program_elf(), &admin.pubkey());
        svm.add_program_from_file(SAS_PROGRAM_ID, format!("{dir}/../../tests/fixtures/sas.so"))
            .expect("load tests/fixtures/sas.so");

        let mint = test_keypair("tusd-mint");
        let mut h = Self {
            svm,
            admin,
            issuer: test_keypair("sas-issuer"),
            treasury: test_keypair("treasury"),
            operator: test_keypair("operator"),
            relayer: test_keypair("relayer"),
            buyer: test_keypair("buyer"),
            sqe: test_keypair("sqe"),
            payout_owner: test_keypair("payout-owner"),
            mint: mint.pubkey(),
            credential: Pubkey::default(),
            schema: Pubkey::default(),
        };
        for actor in h.actors() {
            h.svm.airdrop(&actor, ACTOR_LAMPORTS).expect("airdrop");
        }
        h.create_mint(&mint, None);

        let (credential, schema) = h.create_sas_world(&[h.buyer.pubkey(), h.sqe.pubkey()]);
        h.credential = credential;
        h.schema = schema;
        h
    }

    /// Every actor's public key, in a fixed order.
    pub fn actors(&self) -> Vec<Pubkey> {
        vec![
            self.admin.pubkey(),
            self.issuer.pubkey(),
            self.treasury.pubkey(),
            self.operator.pubkey(),
            self.relayer.pubkey(),
            self.buyer.pubkey(),
            self.sqe.pubkey(),
            self.payout_owner.pubkey(),
        ]
    }

    // ---- transactions ----

    /// A v0 transaction (no lookup table) with the relayer as fee payer, signed by the relayer and
    /// `signers`, at the latest blockhash.
    pub fn tx(&self, ixs: &[Instruction], signers: &[&Keypair]) -> VersionedTransaction {
        let payer = self.relayer.pubkey();
        let message = v0::Message::try_compile(&payer, ixs, &[], self.svm.latest_blockhash())
            .expect("compile v0 message");
        let mut keys: Vec<&Keypair> = vec![&self.relayer];
        for signer in signers {
            if keys.iter().all(|k| k.pubkey() != signer.pubkey()) {
                keys.push(signer);
            }
        }
        VersionedTransaction::try_new(VersionedMessage::V0(message), &keys).expect("sign tx")
    }

    /// Sends `tx`, then expires the blockhash so an identical later transaction is not a duplicate.
    pub fn send(&mut self, tx: VersionedTransaction) -> TransactionResult {
        let result = self.svm.send_transaction(tx);
        self.svm.expire_blockhash();
        result
    }

    /// Sends `tx` and panics with the program logs if it fails.
    pub fn send_ok(&mut self, tx: VersionedTransaction) -> TransactionMetadata {
        self.send(tx).unwrap_or_else(|failed| {
            panic!(
                "transaction failed: {:?}\n{}",
                failed.err,
                failed.meta.pretty_logs()
            )
        })
    }

    /// Sends `tx` and asserts it fails with `InstructionError::Custom(code)` in any instruction,
    /// where `code` is an `EscrowError` (6000 + index) or an Anchor `ErrorCode`.
    pub fn expect_err(
        &mut self,
        tx: VersionedTransaction,
        expected: impl Into<u32>,
    ) -> FailedTransactionMetadata {
        let code = expected.into();
        match self.send(tx) {
            Ok(meta) => panic!(
                "expected custom error {code}, but the transaction succeeded\n{}",
                meta.pretty_logs()
            ),
            Err(failed) => {
                match &failed.err {
                    TransactionError::InstructionError(_, InstructionError::Custom(got))
                        if *got == code => {}
                    other => panic!(
                        "expected custom error {code}, got {other:?}\n{}",
                        failed.meta.pretty_logs()
                    ),
                }
                failed
            }
        }
    }

    /// Sends `tx` and asserts it fails with one of `accepted` in any instruction, for failures that
    /// are not a single custom code (a runtime error, or either of two programs' errors).
    pub fn expect_ix_err(
        &mut self,
        tx: VersionedTransaction,
        accepted: &[InstructionError],
    ) -> FailedTransactionMetadata {
        match self.send(tx) {
            Ok(meta) => panic!(
                "expected one of {accepted:?}, but the transaction succeeded\n{}",
                meta.pretty_logs()
            ),
            Err(failed) => {
                match &failed.err {
                    TransactionError::InstructionError(_, got) if accepted.contains(got) => {}
                    other => panic!(
                        "expected one of {accepted:?}, got {other:?}\n{}",
                        failed.meta.pretty_logs()
                    ),
                }
                failed
            }
        }
    }

    /// Compute units `tx` consumes, by simulation (state unchanged); logged, never asserted.
    pub fn cu(&self, tx: &VersionedTransaction) -> u64 {
        let units = match self.svm.simulate_transaction(tx.clone()) {
            Ok(info) => info.meta.compute_units_consumed,
            Err(failed) => failed.meta.compute_units_consumed,
        };
        println!("cu: {units}");
        units
    }

    /// Advances the `Clock` sysvar's `unix_timestamp` by `secs`.
    pub fn warp(&mut self, secs: i64) {
        let mut clock = self.svm.get_sysvar::<Clock>();
        clock.unix_timestamp += secs;
        self.svm.set_sysvar(&clock);
    }

    pub fn now(&self) -> i64 {
        self.svm.get_sysvar::<Clock>().unix_timestamp
    }

    // ---- tokens ----

    /// Another classic 6-decimal mint of `treasury`, at `test_keypair(label)`.
    pub fn new_mint(&mut self, label: &str) -> Pubkey {
        let mint = test_keypair(label);
        self.create_mint(&mint, None);
        mint.pubkey()
    }

    /// A Circle-like USDC stand-in at `test_keypair(label)`: a classic 6-decimal mint of `treasury`
    /// that, like Circle's devnet USDC and unlike tUSD, keeps a freeze authority (`treasury`).
    pub fn new_circle_like_mint(&mut self, label: &str) -> Pubkey {
        let mint = test_keypair(label);
        let freeze_authority = self.treasury.pubkey();
        self.create_mint(&mint, Some(&freeze_authority));
        mint.pubkey()
    }

    fn create_mint(&mut self, mint: &Keypair, freeze_authority: Option<&Pubkey>) {
        let rent = self.svm.minimum_balance_for_rent_exemption(SPL_MINT_LEN);
        let create = system_instruction::create_account(
            &self.relayer.pubkey(),
            &mint.pubkey(),
            rent,
            SPL_MINT_LEN as u64,
            &token::ID,
        );
        let init = token::spl_token::instruction::initialize_mint2(
            &token::ID,
            &mint.pubkey(),
            &self.treasury.pubkey(),
            freeze_authority,
            MINT_DECIMALS,
        )
        .expect("initialize_mint2");
        let tx = self.tx(&[create, init], &[mint]);
        self.send_ok(tx);
    }

    /// ATA(owner, harness mint).
    pub fn ata(&self, owner: &Pubkey) -> Pubkey {
        associated_token::get_associated_token_address(owner, &self.mint)
    }

    /// Creates ATA(owner, mint) if needed and mints `amount` base units into it; returns the ATA.
    pub fn mint_to(&mut self, owner: &Pubkey, amount: u64) -> Pubkey {
        let ata = self.ata(owner);
        let create =
            associated_token::spl_associated_token_account::instruction::create_associated_token_account_idempotent(
                &self.relayer.pubkey(),
                owner,
                &self.mint,
                &token::ID,
            );
        let mint_to = token::spl_token::instruction::mint_to(
            &token::ID,
            &self.mint,
            &ata,
            &self.treasury.pubkey(),
            &[],
            amount,
        )
        .expect("mint_to");
        let tx = self.tx(&[create, mint_to], &[&self.treasury]);
        self.send_ok(tx);
        ata
    }

    /// Creates ATA(owner, mint) of any classic mint if needed; returns it.
    pub fn create_ata(&mut self, owner: &Pubkey, mint: &Pubkey) -> Pubkey {
        let create =
            associated_token::spl_associated_token_account::instruction::create_associated_token_account_idempotent(
                &self.relayer.pubkey(),
                owner,
                mint,
                &token::ID,
            );
        let tx = self.tx(&[create], &[]);
        self.send_ok(tx);
        associated_token::get_associated_token_address(owner, mint)
    }

    /// Token balance of `token_account` (0 if the account does not exist).
    pub fn token_balance(&self, token_account: &Pubkey) -> u64 {
        self.svm
            .get_account(token_account)
            .map(|account| {
                token::TokenAccount::try_deserialize(&mut account.data.as_slice())
                    .expect("token account")
                    .amount
            })
            .unwrap_or(0)
    }

    /// Rewrites the existing classic token account at `address` with `edit` (`set_account`), to
    /// forge a state no real signer produced, such as a delegate on an escrow's vault.
    pub fn edit_token_account(
        &mut self,
        address: &Pubkey,
        edit: impl FnOnce(&mut token::spl_token::state::Account),
    ) {
        let mut account = self.svm.get_account(address).expect("token account exists");
        let mut state =
            token::spl_token::state::Account::unpack(&account.data).expect("token account");
        edit(&mut state);
        token::spl_token::state::Account::pack(state, &mut account.data).expect("pack");
        self.svm
            .set_account(*address, account)
            .expect("set_account");
    }

    // ---- config ----

    /// `init_config` arguments of the fixture world: the operator and `[mint, empty]`.
    pub fn init_config_args(&self) -> InitConfigArgs {
        InitConfigArgs {
            operator: self.operator.pubkey(),
            allowed_mints: [self.mint, Pubkey::default()],
        }
    }

    /// Writes the fixture world's `Config`, signed by the upgrade authority.
    pub fn init_config(&mut self) {
        let ix = init_config_ix(
            &self.admin.pubkey(),
            self.init_config_args(),
            &self.credential,
            &self.schema,
        );
        let tx = self.tx(&[ix], &[&self.admin]);
        self.send_ok(tx);
    }

    /// The stored `Config`.
    pub fn config(&self) -> Config {
        let account = self
            .svm
            .get_account(&config_address())
            .expect("Config exists");
        Config::try_deserialize(&mut account.data.as_slice()).expect("Config")
    }

    /// This program's current upgrade authority, read from its ProgramData.
    pub fn upgrade_authority(&self) -> Option<Pubkey> {
        let account = self
            .svm
            .get_account(&program_data_address())
            .expect("ProgramData exists");
        ProgramData::try_deserialize(&mut account.data.as_slice())
            .expect("ProgramData")
            .upgrade_authority_address
    }

    // ---- create_po ----

    /// `create_po` arguments of the fixture PO, refundable `ESCROW_REFUND_AFTER_SECONDS` from now.
    pub fn create_po_args(&self) -> CreatePoArgs {
        CreatePoArgs {
            chain_po_id: PO_ID.to_string(),
            po_hash: PO_HASH,
            cad_hash: CAD_HASH,
            total: TOTAL,
            gate_attesters: vec![self.buyer.pubkey(), self.sqe.pubkey()],
            milestones: MILESTONES
                .iter()
                .map(|&(name, bps, gate)| MilestoneInput {
                    name: name.to_string(),
                    bps,
                    gate,
                })
                .collect(),
            refund_after: self.now() + ESCROW_REFUND_AFTER_SECONDS,
        }
    }

    /// `create_po` accounts for `chain_po_id` with the fixture actors: the operator signs, the
    /// relayer pays, and the escrow, vault, payout ATA and event authority are derived.
    pub fn create_po_accounts(&self, chain_po_id: &str) -> bido_escrow::accounts::CreatePo {
        let buyer = self.buyer.pubkey();
        let payout = self.payout_owner.pubkey();
        let escrow = escrow_address(&buyer, chain_po_id);
        bido_escrow::accounts::CreatePo {
            config: config_address(),
            operator: self.operator.pubkey(),
            payer: self.relayer.pubkey(),
            buyer,
            payout,
            mint: self.mint,
            escrow,
            vault: self.ata(&escrow),
            payout_token: self.ata(&payout),
            sas_credential: self.credential,
            token_program: token::ID,
            associated_token_program: associated_token::ID,
            system_program: system_program::ID,
            event_authority: event_authority_address(),
            program: bido_escrow::ID,
        }
    }

    /// A `create_po` transaction for `args` over `accounts`, signed by the operator and the relayer.
    pub fn create_po_tx(
        &self,
        accounts: bido_escrow::accounts::CreatePo,
        args: CreatePoArgs,
    ) -> VersionedTransaction {
        self.tx(&[create_po_ix(accounts, args)], &[&self.operator])
    }

    /// The `create_po` intake transaction in the layout of spec 01 section 3.9, `[CB×2, create_po,
    /// Memo("bido:payout_ref:<hex>")]`, signed by the operator and the relayer (fee and rent).
    pub fn create_po_intake_tx(
        &self,
        accounts: bido_escrow::accounts::CreatePo,
        args: CreatePoArgs,
        payout_ref: &[u8; 32],
    ) -> VersionedTransaction {
        let mut ixs = compute_budget_ixs().to_vec();
        ixs.push(create_po_ix(accounts, args));
        ixs.push(payout_ref_memo_ix(payout_ref));
        self.tx(&ixs, &[&self.operator])
    }

    /// Creates the fixture PO; returns its escrow address.
    pub fn create_po(&mut self) -> Pubkey {
        self.create_po_with_id(PO_ID)
    }

    /// Creates the fixture PO under `chain_po_id` (another escrow of the same buyer, payout,
    /// credential and schema); returns its escrow address.
    pub fn create_po_with_id(&mut self, chain_po_id: &str) -> Pubkey {
        let mut args = self.create_po_args();
        args.chain_po_id = chain_po_id.to_string();
        let accounts = self.create_po_accounts(&args.chain_po_id);
        let escrow = accounts.escrow;
        let tx = self.create_po_tx(accounts, args);
        self.send_ok(tx);
        escrow
    }

    /// The stored `PoEscrow` at `escrow`.
    pub fn escrow(&self, escrow: &Pubkey) -> PoEscrow {
        let account = self.svm.get_account(escrow).expect("PoEscrow exists");
        PoEscrow::try_deserialize(&mut account.data.as_slice()).expect("PoEscrow")
    }

    // ---- fund ----

    /// `fund` accounts for `escrow` signed by `buyer`, paying from ATA(`buyer`, harness mint) into
    /// the escrow's vault.
    pub fn fund_accounts(&self, escrow: &Pubkey, buyer: &Pubkey) -> bido_escrow::accounts::Fund {
        bido_escrow::accounts::Fund {
            buyer: *buyer,
            escrow: *escrow,
            buyer_token: self.ata(buyer),
            vault: self.ata(escrow),
            mint: self.mint,
            token_program: token::ID,
            event_authority: event_authority_address(),
            program: bido_escrow::ID,
        }
    }

    /// A `fund` transaction over `accounts`, signed by `buyer` and the relayer (the fee payer).
    pub fn fund_tx(
        &self,
        accounts: bido_escrow::accounts::Fund,
        buyer: &Keypair,
    ) -> VersionedTransaction {
        self.tx(&[fund_ix(accounts)], &[buyer])
    }

    /// Mints the stored `total` of `escrow` to the buyer's ATA, then funds `escrow` as the buyer.
    pub fn fund(&mut self, escrow: &Pubkey) -> TransactionMetadata {
        let buyer = self.buyer.pubkey();
        self.mint_to(&buyer, self.escrow(escrow).total);
        let tx = self.fund_tx(self.fund_accounts(escrow, &buyer), &self.buyer);
        self.send_ok(tx)
    }

    // ---- approvals and release ----

    /// The fixture key pinned as gate `gate`'s attester of `escrow` (the buyer or the SQE).
    pub fn attester(&self, escrow: &Pubkey, gate: u8) -> &Keypair {
        let pinned = self.escrow(escrow).gates[usize::from(gate)].attester;
        [&self.buyer, &self.sqe]
            .into_iter()
            .find(|key| key.pubkey() == pinned)
            .expect("gate attester is a fixture key")
    }

    /// `release` accounts for `escrow`'s stored vault, payout ATA and mint, passing `attestation`.
    pub fn release_accounts(
        &self,
        escrow: &Pubkey,
        attestation: &Pubkey,
    ) -> bido_escrow::accounts::Release {
        let stored = self.escrow(escrow);
        bido_escrow::accounts::Release {
            escrow: *escrow,
            vault: stored.vault,
            payout_token: stored.payout_token,
            mint: stored.mint,
            attestation: *attestation,
            token_program: token::ID,
            event_authority: event_authority_address(),
            program: bido_escrow::ID,
        }
    }

    /// SAS `CreateAttestation` of gate `gate` of `escrow` signed by `signer` (expiry 0) at the
    /// gate's nonce, under the escrow's credential and schema, for the escrow's `po_hash`; the
    /// relayer pays the rent. Its account 4 is the gate's attestation address.
    pub fn attestation_ix(
        &self,
        escrow: &Pubkey,
        gate: u8,
        signer: &Pubkey,
        evidence_hash: &[u8; 32],
    ) -> Instruction {
        let stored = self.escrow(escrow);
        sas_create_attestation_ix(
            &self.relayer.pubkey(),
            signer,
            &stored.credential,
            &stored.schema,
            &gate_nonce(escrow, gate),
            &gate_approval_data(&stored.po_hash, gate, evidence_hash),
            0,
        )
    }

    /// Gate `gate`'s attestation of `escrow` signed by `signer` on its own, `[CB×2,
    /// CreateAttestation]` with no release: a squat, or an attestation a `release_attempt` cranks.
    pub fn attest_tx(
        &self,
        escrow: &Pubkey,
        gate: u8,
        signer: &Keypair,
        evidence_hash: &[u8; 32],
    ) -> VersionedTransaction {
        let mut ixs = compute_budget_ixs().to_vec();
        ixs.push(self.attestation_ix(escrow, gate, &signer.pubkey(), evidence_hash));
        self.tx(&ixs, &[signer])
    }

    /// Attests gate `gate` of `escrow` as its pinned attester with `fixture_evidence_hash(gate)`,
    /// releasing nothing; returns the gate's stored attestation address, which now holds it.
    pub fn attest(&mut self, escrow: &Pubkey, gate: u8) -> Pubkey {
        let tx = self.attest_tx(
            escrow,
            gate,
            self.attester(escrow, gate),
            &fixture_evidence_hash(gate),
        );
        self.send_ok(tx);
        self.escrow(escrow).gates[usize::from(gate)].attestation
    }

    /// The program instructions of a gate approval (spec 01 section 3.9, without `CB×2`):
    /// `attestation_ix` of gate `gate` signed by `signer`, then `release(m)` with that attestation
    /// for every `Pending` milestone of the gate, in ascending index order.
    pub fn approval_ixs(
        &self,
        escrow: &Pubkey,
        gate: u8,
        signer: &Pubkey,
        evidence_hash: &[u8; 32],
    ) -> Vec<Instruction> {
        let stored = self.escrow(escrow);
        let create = self.attestation_ix(escrow, gate, signer, evidence_hash);
        let attestation = create.accounts[4].pubkey;
        let mut ixs = vec![create];
        for (milestone, m) in (0u8..).zip(&stored.milestones) {
            if m.gate == gate && m.status == MilestoneStatus::Pending {
                ixs.push(release_ix(
                    self.release_accounts(escrow, &attestation),
                    milestone,
                ));
            }
        }
        ixs
    }

    /// A gate approval in the layout of spec 01 section 3.9, `[CB×2, CreateAttestation,
    /// release…]`, signed by `signer` and the relayer (fee and attestation rent).
    pub fn approve_tx(
        &self,
        escrow: &Pubkey,
        gate: u8,
        signer: &Keypair,
        evidence_hash: &[u8; 32],
    ) -> VersionedTransaction {
        let mut ixs = compute_budget_ixs().to_vec();
        ixs.extend(self.approval_ixs(escrow, gate, &signer.pubkey(), evidence_hash));
        self.tx(&ixs, &[signer])
    }

    /// Approves gate `gate` of `escrow` as its pinned attester with `fixture_evidence_hash(gate)`.
    pub fn approve(&mut self, escrow: &Pubkey, gate: u8) -> TransactionMetadata {
        let tx = self.approve_tx(
            escrow,
            gate,
            self.attester(escrow, gate),
            &fixture_evidence_hash(gate),
        );
        self.send_ok(tx)
    }

    /// A `release_attempt` transaction (spec 01 section 3.9), `[CB×2, release(milestone)]` of
    /// `escrow` passing `attestation`, signed by the relayer only.
    pub fn release_with(
        &self,
        escrow: &Pubkey,
        milestone: u8,
        attestation: &Pubkey,
    ) -> VersionedTransaction {
        let mut ixs = compute_budget_ixs().to_vec();
        ixs.push(release_ix(
            self.release_accounts(escrow, attestation),
            milestone,
        ));
        self.tx(&ixs, &[])
    }

    // ---- refund ----

    /// `refund` accounts for `escrow` signed by `buyer`, paying from the escrow's vault into
    /// ATA(`buyer`, harness mint).
    pub fn refund_accounts(
        &self,
        escrow: &Pubkey,
        buyer: &Pubkey,
    ) -> bido_escrow::accounts::Refund {
        bido_escrow::accounts::Refund {
            buyer: *buyer,
            escrow: *escrow,
            vault: self.ata(escrow),
            buyer_token: self.ata(buyer),
            mint: self.mint,
            token_program: token::ID,
            event_authority: event_authority_address(),
            program: bido_escrow::ID,
        }
    }

    /// A `refund` transaction over `accounts`, signed by `buyer` and the relayer (the fee payer).
    pub fn refund_tx(
        &self,
        accounts: bido_escrow::accounts::Refund,
        buyer: &Keypair,
    ) -> VersionedTransaction {
        self.tx(&[refund_ix(accounts)], &[buyer])
    }

    // ---- SAS ----

    /// Creates the credential `bido-approvals` of `issuer` with `signers` and the schema
    /// `bido-gate-approval` v1 under it; returns `(credential, schema)`.
    pub fn create_sas_world(&mut self, signers: &[Pubkey]) -> (Pubkey, Pubkey) {
        let credential = self.create_credential(SAS_CREDENTIAL_NAME, signers);
        let schema = self.create_schema(
            &credential,
            SAS_SCHEMA_NAME,
            &GATE_SCHEMA_LAYOUT,
            &SAS_SCHEMA_FIELDS,
        );
        (credential, schema)
    }

    /// Creates the credential `name` of `issuer` with `signers`; returns its address.
    pub fn create_credential(&mut self, name: &str, signers: &[Pubkey]) -> Pubkey {
        let issuer = self.issuer.pubkey();
        let ix = sas_create_credential_ix(&self.relayer.pubkey(), &issuer, name, signers);
        let tx = self.tx(&[ix], &[&self.issuer]);
        self.send_ok(tx);
        sas_credential_address(&issuer, name)
    }

    /// Creates the schema `name` v1 of `issuer` under `credential`, with the gate schema's
    /// description; returns its address.
    pub fn create_schema(
        &mut self,
        credential: &Pubkey,
        name: &str,
        layout: &[u8],
        field_names: &[&str],
    ) -> Pubkey {
        let ix = sas_create_schema_ix(
            &self.relayer.pubkey(),
            &self.issuer.pubkey(),
            credential,
            name,
            SAS_SCHEMA_DESCRIPTION,
            layout,
            field_names,
        );
        let tx = self.tx(&[ix], &[&self.issuer]);
        self.send_ok(tx);
        sas_schema_address(credential, name, SAS_SCHEMA_VERSION)
    }

    /// Sets exactly `signers` as the authorised signers of `issuer`'s `credential`.
    pub fn change_authorized_signers(&mut self, credential: &Pubkey, signers: &[Pubkey]) {
        let ix = sas_change_authorized_signers_ix(
            &self.relayer.pubkey(),
            &self.issuer.pubkey(),
            credential,
            signers,
        );
        let tx = self.tx(&[ix], &[&self.issuer]);
        self.send_ok(tx);
    }

    /// Pauses or resumes `issuer`'s `schema` under `credential`.
    pub fn set_schema_paused(&mut self, credential: &Pubkey, schema: &Pubkey, is_paused: bool) {
        let ix = sas_change_schema_status_ix(&self.issuer.pubkey(), credential, schema, is_paused);
        let tx = self.tx(&[ix], &[&self.issuer]);
        self.send_ok(tx);
    }
}

impl Default for Harness {
    fn default() -> Self {
        Self::new()
    }
}

/// The ELF `anchor build` wrote to `target/deploy/bido_escrow.so`.
pub fn program_elf() -> Vec<u8> {
    let dir = env!("CARGO_MANIFEST_DIR");
    std::fs::read(format!("{dir}/../../target/deploy/bido_escrow.so"))
        .expect("target/deploy/bido_escrow.so is missing: run `anchor build` first")
}

/// Writes ProgramData (slot 0, `upgrade_authority`, ELF) and Program accounts of the upgradeable
/// loader by hand, so `program_data.upgrade_authority_address` is real for `init_config`.
/// ProgramData goes first: `set_account` on the Program account loads the ELF from it.
pub fn load_upgradeable_program(
    svm: &mut LiteSVM,
    program_id: Pubkey,
    elf: &[u8],
    upgrade_authority: &Pubkey,
) {
    let loader = bpf_loader_upgradeable::ID;
    let program_data = bpf_loader_upgradeable::get_program_data_address(&program_id);

    let mut data = vec![3, 0, 0, 0]; // UpgradeableLoaderState::ProgramData
    data.extend_from_slice(&0u64.to_le_bytes()); // slot
    data.push(1); // Some(upgrade_authority_address)
    data.extend_from_slice(upgrade_authority.as_ref());
    data.extend_from_slice(elf);
    let lamports = svm.minimum_balance_for_rent_exemption(data.len());
    svm.set_account(
        program_data,
        Account {
            lamports,
            data,
            owner: loader,
            executable: false,
            rent_epoch: 0,
        },
    )
    .expect("set ProgramData");

    let mut data = vec![2, 0, 0, 0]; // UpgradeableLoaderState::Program
    data.extend_from_slice(program_data.as_ref());
    let lamports = svm.minimum_balance_for_rent_exemption(data.len());
    svm.set_account(
        program_id,
        Account {
            lamports,
            data,
            owner: loader,
            executable: true,
            rent_epoch: 0,
        },
    )
    .expect("set Program");
}

/// Serialized size of `tx` on the wire (short-vec signature count, signatures, message); logged.
pub fn size(tx: &VersionedTransaction) -> usize {
    let signatures = tx.signatures.len();
    let bytes = short_vec_len(signatures) + 64 * signatures + tx.message.serialize().len();
    println!("size: {bytes} bytes");
    bytes
}

fn short_vec_len(mut n: usize) -> usize {
    let mut len = 1;
    while n >= 0x80 {
        n >>= 7;
        len += 1;
    }
    len
}

// ---- Bido addresses and gate data (spec 01 sections 3.4 and 3.9) ----

/// Config PDA `["config"]`.
pub fn config_address() -> Pubkey {
    Pubkey::find_program_address(&[CONFIG_SEED], &bido_escrow::ID).0
}

/// This program's ProgramData address under the upgradeable loader.
pub fn program_data_address() -> Pubkey {
    bpf_loader_upgradeable::get_program_data_address(&bido_escrow::ID)
}

/// This program's event authority PDA `["__event_authority"]` (`#[event_cpi]`).
pub fn event_authority_address() -> Pubkey {
    Pubkey::find_program_address(&[EVENT_AUTHORITY_SEED], &bido_escrow::ID).0
}

/// Escrow PDA `["po", buyer, chain_po_id]`.
pub fn escrow_address(buyer: &Pubkey, chain_po_id: &str) -> Pubkey {
    Pubkey::find_program_address(
        &[PO_SEED, buyer.as_ref(), chain_po_id.as_bytes()],
        &bido_escrow::ID,
    )
    .0
}

/// Gate nonce `sha256("bido-gate-v1" ‖ escrow ‖ [gate])`, used as the SAS nonce.
pub fn gate_nonce(escrow: &Pubkey, gate: u8) -> Pubkey {
    let hash = solana_sha256_hasher::hashv(&[GATE_NONCE_DOMAIN, escrow.as_ref(), &[gate]]);
    Pubkey::new_from_array(hash.to_bytes())
}

/// Gate approval data (73 bytes): `u32 LE 32 ‖ po_hash ‖ u8 gate ‖ u32 LE 32 ‖ evidence_hash`.
pub fn gate_approval_data(po_hash: &[u8; 32], gate: u8, evidence_hash: &[u8; 32]) -> Vec<u8> {
    let mut data = Vec::with_capacity(GATE_DATA_LEN);
    put_bytes(&mut data, po_hash);
    data.push(gate);
    put_bytes(&mut data, evidence_hash);
    data
}

/// A SAS attestation of the gate schema, decoded at the offsets of spec 01 section 3.6.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GateAttestation {
    pub nonce: Pubkey,
    pub credential: Pubkey,
    pub schema: Pubkey,
    pub po_hash: [u8; 32],
    pub gate: u8,
    pub evidence_hash: [u8; 32],
    pub signer: Pubkey,
    pub expiry: i64,
    pub token_account: Pubkey,
}

impl GateAttestation {
    /// Decodes a 246-byte attestation account; panics on any shape violation of section 3.6.
    pub fn decode(data: &[u8]) -> Self {
        assert_eq!(
            data.len(),
            bido_escrow::ATTESTATION_ACCOUNT_LEN,
            "attestation length"
        );
        assert_eq!(
            data[0],
            bido_escrow::SAS_ATTESTATION_DISC,
            "attestation discriminator"
        );
        assert_eq!(u32_at(data, 97) as usize, GATE_DATA_LEN, "data length");
        assert_eq!(u32_at(data, 101), 32, "po_hash length");
        assert_eq!(u32_at(data, 138), 32, "evidence_hash length");
        Self {
            nonce: key_at(data, 1),
            credential: key_at(data, 33),
            schema: key_at(data, 65),
            po_hash: data[105..137].try_into().unwrap(),
            gate: data[137],
            evidence_hash: data[142..174].try_into().unwrap(),
            signer: key_at(data, 174),
            expiry: i64::from_le_bytes(data[206..214].try_into().unwrap()),
            token_account: key_at(data, 214),
        }
    }

    /// The 246 account bytes SAS writes for these fields, the inverse of `decode`, so a test can
    /// forge an attestation account byte for byte.
    pub fn encode(&self) -> Vec<u8> {
        let mut data = Vec::with_capacity(bido_escrow::ATTESTATION_ACCOUNT_LEN);
        data.push(bido_escrow::SAS_ATTESTATION_DISC);
        data.extend_from_slice(self.nonce.as_ref());
        data.extend_from_slice(self.credential.as_ref());
        data.extend_from_slice(self.schema.as_ref());
        put_bytes(
            &mut data,
            &gate_approval_data(&self.po_hash, self.gate, &self.evidence_hash),
        );
        data.extend_from_slice(self.signer.as_ref());
        data.extend_from_slice(&self.expiry.to_le_bytes());
        data.extend_from_slice(self.token_account.as_ref());
        data
    }
}

/// A SAS credential: `[0] ‖ authority ‖ u32 len ‖ name ‖ u32 count ‖ signers`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SasCredential {
    pub authority: Pubkey,
    pub name: Vec<u8>,
    pub authorized_signers: Vec<Pubkey>,
}

impl SasCredential {
    pub fn decode(data: &[u8]) -> Self {
        assert_eq!(
            data[0],
            bido_escrow::SAS_CREDENTIAL_DISC,
            "credential discriminator"
        );
        let authority = key_at(data, 1);
        let (name, mut at) = bytes_at(data, 33);
        let count = u32_at(data, at) as usize;
        at += 4;
        let authorized_signers = (0..count).map(|i| key_at(data, at + 32 * i)).collect();
        Self {
            authority,
            name,
            authorized_signers,
        }
    }
}

/// A SAS schema: `[1] ‖ credential ‖ name ‖ description ‖ layout ‖ field_names ‖ is_paused ‖ version`,
/// each variable field a u32 byte length then the bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SasSchema {
    pub credential: Pubkey,
    pub name: Vec<u8>,
    pub description: Vec<u8>,
    pub layout: Vec<u8>,
    /// Field names, each stored as `u32 len ‖ utf-8`.
    pub field_names: Vec<String>,
    pub is_paused: bool,
    pub version: u8,
}

impl SasSchema {
    pub fn decode(data: &[u8]) -> Self {
        assert_eq!(
            data[0],
            bido_escrow::SAS_SCHEMA_DISC,
            "schema discriminator"
        );
        let credential = key_at(data, 1);
        let (name, at) = bytes_at(data, 33);
        let (description, at) = bytes_at(data, at);
        let (layout, at) = bytes_at(data, at);
        let (names, at) = bytes_at(data, at);
        let mut field_names = Vec::new();
        let mut i = 0;
        while i < names.len() {
            let (field, next) = bytes_at(&names, i);
            field_names.push(String::from_utf8(field).expect("utf-8 field name"));
            i = next;
        }
        Self {
            credential,
            name,
            description,
            layout,
            field_names,
            is_paused: data[at] != 0,
            version: data[at + 1],
        }
    }
}

fn u32_at(data: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(data[at..at + 4].try_into().unwrap())
}

fn key_at(data: &[u8], at: usize) -> Pubkey {
    Pubkey::new_from_array(data[at..at + 32].try_into().unwrap())
}

/// A u32-length-prefixed byte string at `at`; returns it and the offset after it.
fn bytes_at(data: &[u8], at: usize) -> (Vec<u8>, usize) {
    let len = u32_at(data, at) as usize;
    (data[at + 4..at + 4 + len].to_vec(), at + 4 + len)
}

// ---- Bido config instructions ----

/// `init_config` signed by `admin`: `[admin (w, s), config (w), program, program_data,
/// sas_credential, sas_schema, system_program]` (spec 01 section 3.5).
pub fn init_config_ix(
    admin: &Pubkey,
    args: InitConfigArgs,
    credential: &Pubkey,
    schema: &Pubkey,
) -> Instruction {
    Instruction {
        program_id: bido_escrow::ID,
        accounts: bido_escrow::accounts::InitConfig {
            admin: *admin,
            config: config_address(),
            program: bido_escrow::ID,
            program_data: program_data_address(),
            sas_credential: *credential,
            sas_schema: *schema,
            system_program: system_program::ID,
        }
        .to_account_metas(None),
        data: bido_escrow::instruction::InitConfig { args }.data(),
    }
}

/// `update_config` signed by `admin`: `[admin (s), config (w), program, program_data,
/// sas_credential, sas_schema]`, where the SAS accounts are the resulting pair.
pub fn update_config_ix(
    admin: &Pubkey,
    args: UpdateConfigArgs,
    credential: &Pubkey,
    schema: &Pubkey,
) -> Instruction {
    Instruction {
        program_id: bido_escrow::ID,
        accounts: bido_escrow::accounts::UpdateConfig {
            admin: *admin,
            config: config_address(),
            program: bido_escrow::ID,
            program_data: program_data_address(),
            sas_credential: *credential,
            sas_schema: *schema,
        }
        .to_account_metas(None),
        data: bido_escrow::instruction::UpdateConfig { args }.data(),
    }
}

// ---- Bido PO instructions ----

/// `create_po` signed by `accounts.operator` and `accounts.payer`: `[config, operator (s), payer
/// (w, s), buyer, payout, mint, escrow (w), vault (w), payout_token (w), sas_credential,
/// token_program, associated_token_program, system_program, event_authority, program]`.
pub fn create_po_ix(accounts: bido_escrow::accounts::CreatePo, args: CreatePoArgs) -> Instruction {
    Instruction {
        program_id: bido_escrow::ID,
        accounts: accounts.to_account_metas(None),
        data: bido_escrow::instruction::CreatePo { args }.data(),
    }
}

/// `fund` signed by `accounts.buyer`: `[buyer (s), escrow (w), buyer_token (w), vault (w), mint,
/// token_program, event_authority, program]`.
pub fn fund_ix(accounts: bido_escrow::accounts::Fund) -> Instruction {
    Instruction {
        program_id: bido_escrow::ID,
        accounts: accounts.to_account_metas(None),
        data: bido_escrow::instruction::Fund {}.data(),
    }
}

/// `release(milestone)`, signed by no one but the fee payer: `[escrow (w), vault (w), payout_token
/// (w), mint, attestation, token_program, event_authority, program]`.
pub fn release_ix(accounts: bido_escrow::accounts::Release, milestone: u8) -> Instruction {
    Instruction {
        program_id: bido_escrow::ID,
        accounts: accounts.to_account_metas(None),
        data: bido_escrow::instruction::Release { milestone }.data(),
    }
}

/// `refund` signed by `accounts.buyer`: `[buyer (s), escrow (w), vault (w), buyer_token (w), mint,
/// token_program, event_authority, program]`.
pub fn refund_ix(accounts: bido_escrow::accounts::Refund) -> Instruction {
    Instruction {
        program_id: bido_escrow::ID,
        accounts: accounts.to_account_metas(None),
        data: bido_escrow::instruction::Refund {}.data(),
    }
}

// ---- compute budget ----

/// `CB×2` of the harness: `[SetComputeUnitLimit(HARNESS_CU_LIMIT),
/// SetComputeUnitPrice(HARNESS_CU_PRICE)]`, encoded by hand.
pub fn compute_budget_ixs() -> [Instruction; 2] {
    let mut limit = vec![CB_IX_SET_COMPUTE_UNIT_LIMIT];
    limit.extend_from_slice(&HARNESS_CU_LIMIT.to_le_bytes());
    let mut price = vec![CB_IX_SET_COMPUTE_UNIT_PRICE];
    price.extend_from_slice(&HARNESS_CU_PRICE.to_le_bytes());
    [limit, price].map(|data| Instruction {
        program_id: COMPUTE_BUDGET_PROGRAM_ID,
        accounts: vec![],
        data,
    })
}

// ---- memo ----

/// The SPL Memo `bido:payout_ref:<hex>` (80 bytes), with no accounts, so it needs no signer.
pub fn payout_ref_memo_ix(payout_ref: &[u8; 32]) -> Instruction {
    let hex: String = payout_ref.iter().map(|b| format!("{b:02x}")).collect();
    Instruction {
        program_id: MEMO_PROGRAM_ID,
        accounts: vec![],
        data: format!("{PAYOUT_REF_MEMO_PREFIX}{hex}").into_bytes(),
    }
}

// ---- Bido events ----

/// One event a transaction emitted by self-CPI into this program (`emit_cpi!`, spec 01 3.8).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EmittedEvent {
    /// Index of the top-level instruction whose execution emitted it.
    pub instruction: usize,
    /// Invocation stack height of the self-CPI; 2 when the top-level instruction emitted it.
    pub stack_height: u8,
    /// The event's discriminator, then its Borsh fields (`Event::data()`).
    pub payload: Vec<u8>,
}

/// Every event `tx` emitted by self-CPI into this program, in order; asserts that each self-CPI
/// is an event instruction whose only account is the event authority.
pub fn emitted_events(tx: &VersionedTransaction, meta: &TransactionMetadata) -> Vec<EmittedEvent> {
    let keys = tx.message.static_account_keys();
    let mut events = Vec::new();
    for (instruction, inner) in meta.inner_instructions.iter().enumerate() {
        for inner in inner {
            let ix = &inner.instruction;
            if keys[usize::from(ix.program_id_index)] != bido_escrow::ID {
                continue;
            }
            let payload = ix
                .data
                .strip_prefix(EVENT_IX_TAG_LE)
                .expect("a self-CPI into bido_escrow is an event");
            let accounts: Vec<Pubkey> = ix.accounts.iter().map(|&i| keys[usize::from(i)]).collect();
            assert_eq!(accounts, [event_authority_address()], "event authority");
            events.push(EmittedEvent {
                instruction,
                stack_height: inner.stack_height,
                payload: payload.to_vec(),
            });
        }
    }
    events
}

/// Every `E` event `tx` emitted by self-CPI into this program, in order (spec 01 section 3.8),
/// decoded with no byte left over; asserts as `emitted_events` does.
pub fn emitted<E: AnchorDeserialize + Discriminator>(
    tx: &VersionedTransaction,
    meta: &TransactionMetadata,
) -> Vec<E> {
    emitted_events(tx, meta)
        .iter()
        .filter_map(|event| {
            let body = event.payload.strip_prefix(E::DISCRIMINATOR)?;
            Some(E::try_from_slice(body).expect("event"))
        })
        .collect()
}

// ---- SAS addresses and instructions, encoded by hand ----

/// Credential PDA `["credential", authority, name]`.
pub fn sas_credential_address(authority: &Pubkey, name: &str) -> Pubkey {
    Pubkey::find_program_address(
        &[SAS_CREDENTIAL_SEED, authority.as_ref(), name.as_bytes()],
        &SAS_PROGRAM_ID,
    )
    .0
}

/// Schema PDA `["schema", credential, name, [version]]`.
pub fn sas_schema_address(credential: &Pubkey, name: &str, version: u8) -> Pubkey {
    Pubkey::find_program_address(
        &[
            SAS_SCHEMA_SEED,
            credential.as_ref(),
            name.as_bytes(),
            &[version],
        ],
        &SAS_PROGRAM_ID,
    )
    .0
}

/// Attestation PDA `["attestation", credential, schema, nonce]`.
pub fn sas_attestation_address(credential: &Pubkey, schema: &Pubkey, nonce: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(
        &[
            SAS_ATTESTATION_SEED,
            credential.as_ref(),
            schema.as_ref(),
            nonce.as_ref(),
        ],
        &SAS_PROGRAM_ID,
    )
    .0
}

/// `CreateCredential` (tag 0): `[payer (w, s), credential (w), authority (s), system_program]`.
pub fn sas_create_credential_ix(
    payer: &Pubkey,
    authority: &Pubkey,
    name: &str,
    signers: &[Pubkey],
) -> Instruction {
    let mut data = vec![SAS_IX_CREATE_CREDENTIAL];
    put_bytes(&mut data, name.as_bytes());
    put_keys(&mut data, signers);
    Instruction {
        program_id: SAS_PROGRAM_ID,
        accounts: vec![
            AccountMeta::new(*payer, true),
            AccountMeta::new(sas_credential_address(authority, name), false),
            AccountMeta::new_readonly(*authority, true),
            AccountMeta::new_readonly(system_program::ID, false),
        ],
        data,
    }
}

/// `CreateSchema` (tag 1), version 1: `[payer (w, s), authority (s), credential, schema (w),
/// system_program]`; field names are a u32 count, then each name u32-length-prefixed.
pub fn sas_create_schema_ix(
    payer: &Pubkey,
    authority: &Pubkey,
    credential: &Pubkey,
    name: &str,
    description: &str,
    layout: &[u8],
    field_names: &[&str],
) -> Instruction {
    let mut data = vec![SAS_IX_CREATE_SCHEMA];
    put_bytes(&mut data, name.as_bytes());
    put_bytes(&mut data, description.as_bytes());
    put_bytes(&mut data, layout);
    data.extend_from_slice(&(field_names.len() as u32).to_le_bytes());
    for field in field_names {
        put_bytes(&mut data, field.as_bytes());
    }
    Instruction {
        program_id: SAS_PROGRAM_ID,
        accounts: vec![
            AccountMeta::new(*payer, true),
            AccountMeta::new_readonly(*authority, true),
            AccountMeta::new_readonly(*credential, false),
            AccountMeta::new(
                sas_schema_address(credential, name, SAS_SCHEMA_VERSION),
                false,
            ),
            AccountMeta::new_readonly(system_program::ID, false),
        ],
        data,
    }
}

/// `ChangeSchemaStatus` (tag 2): `[authority (s), credential, schema (w)]`, data `[2, is_paused]`.
pub fn sas_change_schema_status_ix(
    authority: &Pubkey,
    credential: &Pubkey,
    schema: &Pubkey,
    is_paused: bool,
) -> Instruction {
    Instruction {
        program_id: SAS_PROGRAM_ID,
        accounts: vec![
            AccountMeta::new_readonly(*authority, true),
            AccountMeta::new_readonly(*credential, false),
            AccountMeta::new(*schema, false),
        ],
        data: vec![SAS_IX_CHANGE_SCHEMA_STATUS, is_paused as u8],
    }
}

/// `ChangeAuthorizedSigners` (tag 3), sets exactly `signers`: `[payer (w, s), authority (s),
/// credential (w), system_program]`.
pub fn sas_change_authorized_signers_ix(
    payer: &Pubkey,
    authority: &Pubkey,
    credential: &Pubkey,
    signers: &[Pubkey],
) -> Instruction {
    let mut data = vec![SAS_IX_CHANGE_AUTHORIZED_SIGNERS];
    put_keys(&mut data, signers);
    Instruction {
        program_id: SAS_PROGRAM_ID,
        accounts: vec![
            AccountMeta::new(*payer, true),
            AccountMeta::new_readonly(*authority, true),
            AccountMeta::new(*credential, false),
            AccountMeta::new_readonly(system_program::ID, false),
        ],
        data,
    }
}

/// `CreateAttestation` (tag 6), 118 bytes for gate data: `0x06 ‖ nonce ‖ u32 LE len ‖ data ‖
/// i64 LE expiry`; accounts `[payer (w, s), authorized_signer (s), credential, schema,
/// attestation (w), system_program]` (spec 01 section 3.9).
pub fn sas_create_attestation_ix(
    payer: &Pubkey,
    signer: &Pubkey,
    credential: &Pubkey,
    schema: &Pubkey,
    nonce: &Pubkey,
    attestation_data: &[u8],
    expiry: i64,
) -> Instruction {
    let mut data = vec![SAS_IX_CREATE_ATTESTATION];
    data.extend_from_slice(nonce.as_ref());
    put_bytes(&mut data, attestation_data);
    data.extend_from_slice(&expiry.to_le_bytes());
    Instruction {
        program_id: SAS_PROGRAM_ID,
        accounts: vec![
            AccountMeta::new(*payer, true),
            AccountMeta::new_readonly(*signer, true),
            AccountMeta::new_readonly(*credential, false),
            AccountMeta::new_readonly(*schema, false),
            AccountMeta::new(sas_attestation_address(credential, schema, nonce), false),
            AccountMeta::new_readonly(system_program::ID, false),
        ],
        data,
    }
}

/// `CloseAttestation` (tag 7), rent back to `payer`: `[payer (w, s), authorized_signer (s),
/// credential, attestation (w), SAS event authority, system_program, SAS program]`.
pub fn sas_close_attestation_ix(
    payer: &Pubkey,
    signer: &Pubkey,
    credential: &Pubkey,
    attestation: &Pubkey,
) -> Instruction {
    Instruction {
        program_id: SAS_PROGRAM_ID,
        accounts: vec![
            AccountMeta::new(*payer, true),
            AccountMeta::new_readonly(*signer, true),
            AccountMeta::new_readonly(*credential, false),
            AccountMeta::new(*attestation, false),
            AccountMeta::new_readonly(SAS_EVENT_AUTHORITY, false),
            AccountMeta::new_readonly(system_program::ID, false),
            AccountMeta::new_readonly(SAS_PROGRAM_ID, false),
        ],
        data: vec![SAS_IX_CLOSE_ATTESTATION],
    }
}

fn put_bytes(out: &mut Vec<u8>, bytes: &[u8]) {
    out.extend_from_slice(&(bytes.len() as u32).to_le_bytes());
    out.extend_from_slice(bytes);
}

fn put_keys(out: &mut Vec<u8>, keys: &[Pubkey]) {
    out.extend_from_slice(&(keys.len() as u32).to_le_bytes());
    for key in keys {
        out.extend_from_slice(key.as_ref());
    }
}
