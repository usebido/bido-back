use anchor_lang::prelude::*;
use anchor_spl::token_interface::{
    transfer_checked, Mint, TokenAccount, TokenInterface, TransferChecked,
};

use crate::{
    approval::verify_approval,
    constants::PO_SEED,
    errors::EscrowError,
    events::{MilestoneReleased, PoCompleted},
    state::{EscrowStatus, MilestoneStatus, PoEscrow},
};

/// Permissionless: anyone may submit it; the money can only reach `payout_token`.
/// The account constraints are phase A of the verification algorithm (spec 01 section 3.6).
#[event_cpi]
#[derive(Accounts)]
pub struct Release<'info> {
    #[account(mut)]
    pub escrow: Box<Account<'info, PoEscrow>>,
    #[account(mut, address = escrow.vault @ EscrowError::VaultAccountInvalid)]
    pub vault: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(
        mut,
        address = escrow.payout_token @ EscrowError::PayoutMismatch,
        constraint = payout_token.owner == escrow.payout @ EscrowError::PayoutMismatch,
        constraint = payout_token.mint == escrow.mint @ EscrowError::PayoutMismatch,
    )]
    pub payout_token: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(address = escrow.mint @ EscrowError::MintNotAllowed)]
    pub mint: Box<InterfaceAccount<'info, Mint>>,
    /// CHECK: the gate's SAS attestation; `verify_approval` checks its address, owner, shape and fields.
    pub attestation: UncheckedAccount<'info>,
    pub token_program: Interface<'info, TokenInterface>,
}

/// Phase B of spec 01 section 3.6: checks 1-3 here, checks 4-13 in `verify_approval`; then the
/// effects, then the PDA-signed transfer of the milestone amount, then the events.
pub fn handle_release(ctx: Context<Release>, milestone: u8) -> Result<()> {
    let escrow_key = ctx.accounts.escrow.key();
    let clock = Clock::get()?;
    let escrow = &mut ctx.accounts.escrow;

    // 1.
    match escrow.status {
        EscrowStatus::Funded => {}
        EscrowStatus::Created => return err!(EscrowError::NotFunded),
        EscrowStatus::Completed | EscrowStatus::Refunded => return err!(EscrowError::EscrowClosed),
    }

    // 2.
    let index = usize::from(milestone);
    let pending = escrow
        .milestones
        .get(index)
        .ok_or(EscrowError::InvalidMilestone)?;

    // 3.
    require!(
        pending.status == MilestoneStatus::Pending,
        EscrowError::MilestoneAlreadyReleased
    );
    let gate = pending.gate;
    let amount = pending.amount;

    // 4-13.
    let approval = verify_approval(
        escrow,
        &escrow_key,
        gate,
        &ctx.accounts.attestation,
        clock.unix_timestamp,
    )?;

    // Effects.
    let released = &mut escrow.milestones[index];
    released.status = MilestoneStatus::Released;
    released.released_slot = clock.slot;
    released.approved_by = approval.signer;
    released.evidence_hash = approval.evidence_hash;
    escrow.released = escrow
        .released
        .checked_add(amount)
        .ok_or(EscrowError::MathOverflow)?;
    let completed = escrow
        .milestones
        .iter()
        .all(|m| m.status == MilestoneStatus::Released);
    if completed {
        escrow.status = EscrowStatus::Completed;
    }

    // Interaction: the vault's authority is the escrow PDA.
    let released_event = MilestoneReleased {
        escrow: escrow_key,
        chain_po_id: escrow.chain_po_id.clone(),
        milestone,
        gate,
        amount,
        released_total: escrow.released,
        payout: escrow.payout,
        payout_token: escrow.payout_token,
        attestation: ctx.accounts.attestation.key(),
        approved_by: approval.signer,
        evidence_hash: approval.evidence_hash,
        slot: clock.slot,
    };
    let completed_event = completed.then(|| PoCompleted {
        escrow: escrow_key,
        chain_po_id: escrow.chain_po_id.clone(),
        total: escrow.total,
        slot: clock.slot,
    });
    let buyer = escrow.buyer;
    let bump = [escrow.bump];
    let seeds: &[&[u8]] = &[
        PO_SEED,
        buyer.as_ref(),
        escrow.chain_po_id.as_bytes(),
        &bump,
    ];
    transfer_checked(
        CpiContext::new_with_signer(
            ctx.accounts.token_program.key(),
            TransferChecked {
                from: ctx.accounts.vault.to_account_info(),
                mint: ctx.accounts.mint.to_account_info(),
                to: ctx.accounts.payout_token.to_account_info(),
                authority: escrow.to_account_info(),
            },
            &[seeds],
        ),
        amount,
        ctx.accounts.mint.decimals,
    )?;

    emit_cpi!(released_event);
    if let Some(event) = completed_event {
        emit_cpi!(event);
    }
    Ok(())
}
