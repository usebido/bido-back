use anchor_lang::prelude::*;
use anchor_spl::{
    associated_token::AssociatedToken,
    token_interface::{Mint, TokenAccount, TokenInterface},
};
use solana_sha256_hasher::hashv;

use crate::{
    constants::{
        BPS_TOTAL, CONFIG_SEED, GATE_NONCE_DOMAIN, MAX_CHAIN_PO_ID_LEN, MAX_GATES, MAX_MILESTONES,
        MAX_MILESTONE_NAME_LEN, PO_SEED, SAS_PROGRAM_ID,
    },
    errors::EscrowError,
    events::PoCreated,
    sas,
    state::{Config, EscrowStatus, Gate, Milestone, MilestoneStatus, PoEscrow},
};

/// Shortest valid `chain_po_id` in bytes (spec 01 section 3.5.1 check 3).
const MIN_CHAIN_PO_ID_LEN: usize = 6;
/// First seed of a SAS attestation PDA `["attestation", credential, schema, nonce]`.
const SAS_ATTESTATION_SEED: &[u8] = b"attestation";

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Debug, PartialEq, Eq)]
pub struct MilestoneInput {
    pub name: String,
    pub bps: u16,
    pub gate: u8,
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Debug, PartialEq, Eq)]
pub struct CreatePoArgs {
    pub chain_po_id: String,
    pub po_hash: [u8; 32],
    pub cad_hash: [u8; 32],
    pub total: u64,
    pub gate_attesters: Vec<Pubkey>,
    pub milestones: Vec<MilestoneInput>,
    pub refund_after: i64,
}

#[event_cpi]
#[derive(Accounts)]
#[instruction(args: CreatePoArgs)]
pub struct CreatePo<'info> {
    #[account(seeds = [CONFIG_SEED], bump = config.bump)]
    pub config: Box<Account<'info, Config>>,
    #[account(address = config.operator @ EscrowError::UnauthorizedOperator)]
    pub operator: Signer<'info>,
    /// The relayer: pays the fee and the rent of the escrow and both ATAs.
    #[account(mut)]
    pub payer: Signer<'info>,
    /// CHECK: buyer wallet; only its key is stored (it signs `fund` and `refund`).
    pub buyer: UncheckedAccount<'info>,
    /// CHECK: payout owner wallet from the verified registry; only its key is stored.
    pub payout: UncheckedAccount<'info>,
    pub mint: Box<InterfaceAccount<'info, Mint>>,
    #[account(
        init,
        payer = payer,
        space = PoEscrow::DISCRIMINATOR.len() + PoEscrow::INIT_SPACE,
        seeds = [PO_SEED, buyer.key().as_ref(), args.chain_po_id.as_bytes()],
        bump,
    )]
    pub escrow: Box<Account<'info, PoEscrow>>,
    #[account(
        init_if_needed,
        payer = payer,
        associated_token::mint = mint,
        associated_token::authority = escrow,
        associated_token::token_program = token_program,
    )]
    pub vault: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(
        init_if_needed,
        payer = payer,
        associated_token::mint = mint,
        associated_token::authority = payout,
        associated_token::token_program = token_program,
    )]
    pub payout_token: Box<InterfaceAccount<'info, TokenAccount>>,
    /// CHECK: equals `config.sas_credential`; the handler reads its authorized signers.
    #[account(address = config.sas_credential)]
    pub sas_credential: UncheckedAccount<'info>,
    pub token_program: Interface<'info, TokenInterface>,
    pub associated_token_program: Program<'info, AssociatedToken>,
    pub system_program: Program<'info, System>,
}

/// Runs the validation of spec 01 section 3.5.1 in order (check 1, the operator, is the account
/// constraint on `operator`), then writes the `Created` escrow and emits `PoCreated`. Anchor runs
/// the account constraints first, so a duplicate id fails in the System program and an id over 32
/// bytes fails in the escrow PDA derivation, before check 1.
pub fn handle_create_po(ctx: Context<CreatePo>, args: CreatePoArgs) -> Result<()> {
    let config = &ctx.accounts.config;
    let mint = ctx.accounts.mint.key();
    let payout = ctx.accounts.payout.key();

    // 2. An allowed mint, owned by the token program passed.
    require!(
        mint != Pubkey::default() && config.allowed_mints.contains(&mint),
        EscrowError::MintNotAllowed
    );
    require_keys_eq!(
        *ctx.accounts.mint.to_account_info().owner,
        ctx.accounts.token_program.key(),
        EscrowError::MintNotAllowed
    );

    // 3.
    require!(
        is_valid_chain_po_id(&args.chain_po_id),
        EscrowError::InvalidPoId
    );

    // 4.
    require!(
        (1..=MAX_MILESTONES).contains(&args.milestones.len()),
        EscrowError::InvalidMilestoneCount
    );

    // 5.
    require!(
        args.milestones
            .iter()
            .all(|m| (1..=MAX_MILESTONE_NAME_LEN).contains(&m.name.len())),
        EscrowError::NameTooLong
    );

    // 6.
    require!(
        args.milestones.iter().all(|m| m.bps > 0),
        EscrowError::ZeroBps
    );
    let bps_sum: u32 = args.milestones.iter().map(|m| u32::from(m.bps)).sum();
    require!(bps_sum == u32::from(BPS_TOTAL), EscrowError::BpsSumNot10000);

    // 7.
    let amounts = milestone_amounts(args.total, &args.milestones)?;

    // 8. Every milestone names an existing gate and every gate is used.
    let gate_count = args.gate_attesters.len();
    require!(
        (1..=MAX_GATES).contains(&gate_count),
        EscrowError::InvalidGate
    );
    require!(
        args.milestones
            .iter()
            .all(|m| usize::from(m.gate) < gate_count),
        EscrowError::InvalidGate
    );
    require!(
        (0..gate_count).all(|g| args.milestones.iter().any(|m| usize::from(m.gate) == g)),
        EscrowError::InvalidGate
    );

    // 9.
    require!(
        args.gate_attesters
            .iter()
            .all(|attester| *attester != Pubkey::default() && *attester != payout),
        EscrowError::InvalidAttester
    );

    // 10. Every attester can sign a SAS attestation under the configured credential.
    let signers = sas::parse_credential_signers(&ctx.accounts.sas_credential)
        .ok_or(EscrowError::GateAttesterMismatch)?;
    require!(
        args.gate_attesters
            .iter()
            .all(|attester| signers.contains(attester)),
        EscrowError::GateAttesterMismatch
    );

    // 11. `payout == escrow` makes `payout_token` the vault, which Anchor's duplicate mutable
    // account check rejects before the handler; the check here still covers it.
    let escrow = ctx.accounts.escrow.key();
    let vault = ctx.accounts.vault.key();
    require!(
        payout != escrow && payout != vault && ctx.accounts.payout_token.key() != vault,
        EscrowError::PayoutIsEscrow
    );

    // 12.
    require_keys_neq!(payout, ctx.accounts.buyer.key(), EscrowError::PayoutIsBuyer);

    // 13. A vault created before this instruction (`init_if_needed`) must still be clean.
    require!(
        ctx.accounts.vault.delegate.is_none() && ctx.accounts.vault.close_authority.is_none(),
        EscrowError::VaultAccountInvalid
    );

    // 14.
    let clock = Clock::get()?;
    require!(
        args.refund_after > clock.unix_timestamp,
        EscrowError::InvalidRefundDeadline
    );

    // The escrow is pinned to the credential and schema configured now; a later
    // `update_config` never changes them.
    let credential = config.sas_credential;
    let schema = config.sas_schema;
    let gates: Vec<Gate> = args
        .gate_attesters
        .iter()
        .zip(0u8..)
        .map(|(attester, gate)| Gate {
            attester: *attester,
            attestation: gate_attestation_address(&credential, &schema, &gate_nonce(&escrow, gate)),
        })
        .collect();
    let milestones: Vec<Milestone> = args
        .milestones
        .iter()
        .zip(&amounts)
        .map(|(input, amount)| Milestone {
            name: input.name.clone(),
            bps: input.bps,
            amount: *amount,
            gate: input.gate,
            status: MilestoneStatus::Pending,
            released_slot: 0,
            approved_by: Pubkey::default(),
            evidence_hash: [0; 32],
        })
        .collect();

    let event = PoCreated {
        escrow,
        chain_po_id: args.chain_po_id.clone(),
        buyer: ctx.accounts.buyer.key(),
        payout,
        payout_token: ctx.accounts.payout_token.key(),
        mint,
        vault,
        po_hash: args.po_hash,
        cad_hash: args.cad_hash,
        total: args.total,
        credential,
        schema,
        gate_attesters: args.gate_attesters.clone(),
        gate_attestations: gates.iter().map(|gate| gate.attestation).collect(),
        milestone_names: args.milestones.iter().map(|m| m.name.clone()).collect(),
        milestone_bps: args.milestones.iter().map(|m| m.bps).collect(),
        milestone_gates: args.milestones.iter().map(|m| m.gate).collect(),
        milestone_amounts: amounts,
        refund_after: args.refund_after,
        slot: clock.slot,
    };

    ctx.accounts.escrow.set_inner(PoEscrow {
        chain_po_id: args.chain_po_id,
        buyer: event.buyer,
        payout,
        payout_token: event.payout_token,
        mint,
        vault,
        po_hash: args.po_hash,
        cad_hash: args.cad_hash,
        total: args.total,
        released: 0,
        status: EscrowStatus::Created,
        credential,
        schema,
        gates,
        milestones,
        refund_after: args.refund_after,
        bump: ctx.bumps.escrow,
    });
    emit_cpi!(event);
    Ok(())
}

/// Check 3: 6 to 32 bytes, each in `[A-Z0-9-]` (the full `CHAIN_PO_ID_RE` is enforced off-chain).
fn is_valid_chain_po_id(chain_po_id: &str) -> bool {
    (MIN_CHAIN_PO_ID_LEN..=MAX_CHAIN_PO_ID_LEN).contains(&chain_po_id.len())
        && chain_po_id
            .bytes()
            .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'-')
}

/// Check 7 and spec 01 section 3.4: milestone `i < n-1` gets `floor(total × bps_i / 10_000)` in
/// `u128`, the last one the rest, so the amounts add up to `total`; `total` and every amount must
/// be greater than zero.
fn milestone_amounts(total: u64, milestones: &[MilestoneInput]) -> Result<Vec<u64>> {
    require!(total > 0, EscrowError::ZeroAmount);
    let (_, leading) = milestones
        .split_last()
        .ok_or(EscrowError::InvalidMilestoneCount)?;
    let mut amounts = Vec::with_capacity(milestones.len());
    let mut allocated: u64 = 0;
    for milestone in leading {
        let share = u128::from(total)
            .checked_mul(u128::from(milestone.bps))
            .ok_or(EscrowError::MathOverflow)?
            / u128::from(BPS_TOTAL);
        let amount = u64::try_from(share).map_err(|_| EscrowError::MathOverflow)?;
        allocated = allocated
            .checked_add(amount)
            .ok_or(EscrowError::MathOverflow)?;
        amounts.push(amount);
    }
    amounts.push(
        total
            .checked_sub(allocated)
            .ok_or(EscrowError::MathOverflow)?,
    );
    require!(
        amounts.iter().all(|amount| *amount > 0),
        EscrowError::ZeroAmount
    );
    Ok(amounts)
}

/// Gate nonce `sha256("bido-gate-v1" ‖ escrow ‖ [gate])`: the SAS nonce of the gate's attestation.
pub fn gate_nonce(escrow: &Pubkey, gate: u8) -> Pubkey {
    Pubkey::new_from_array(hashv(&[GATE_NONCE_DOMAIN, escrow.as_ref(), &[gate]]).to_bytes())
}

/// The SAS attestation PDA `["attestation", credential, schema, nonce]` a gate expects.
pub fn gate_attestation_address(credential: &Pubkey, schema: &Pubkey, nonce: &Pubkey) -> Pubkey {
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
