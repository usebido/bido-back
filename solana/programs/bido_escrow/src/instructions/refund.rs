use anchor_lang::prelude::*;
use anchor_spl::token_interface::{
    transfer_checked, Mint, TokenAccount, TokenInterface, TransferChecked,
};

use crate::{
    constants::PO_SEED,
    errors::EscrowError,
    events::PoRefunded,
    state::{EscrowStatus, MilestoneStatus, PoEscrow},
};

#[event_cpi]
#[derive(Accounts)]
pub struct Refund<'info> {
    #[account(address = escrow.buyer @ EscrowError::UnauthorizedBuyer)]
    pub buyer: Signer<'info>,
    #[account(mut)]
    pub escrow: Box<Account<'info, PoEscrow>>,
    #[account(mut, address = escrow.vault @ EscrowError::VaultAccountInvalid)]
    pub vault: Box<InterfaceAccount<'info, TokenAccount>>,
    /// ATA(buyer, mint); must already exist.
    #[account(
        mut,
        associated_token::mint = mint,
        associated_token::authority = buyer,
        associated_token::token_program = token_program,
    )]
    pub buyer_token: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(address = escrow.mint @ EscrowError::MintNotAllowed)]
    pub mint: Box<InterfaceAccount<'info, Mint>>,
    pub token_program: Interface<'info, TokenInterface>,
}

/// Returns `total − released` from the vault to ATA(buyer, mint) once `refund_after` has passed
/// (the buyer check is the account constraint on `buyer`). Every pending milestone is cancelled,
/// so no later `release` can pay it; a donation in the vault beyond the total stays there.
pub fn handle_refund(ctx: Context<Refund>) -> Result<()> {
    let escrow_key = ctx.accounts.escrow.key();
    let clock = Clock::get()?;
    let escrow = &mut ctx.accounts.escrow;

    match escrow.status {
        EscrowStatus::Funded => {}
        EscrowStatus::Created => return err!(EscrowError::NotFunded),
        EscrowStatus::Completed | EscrowStatus::Refunded => return err!(EscrowError::EscrowClosed),
    }
    require!(
        clock.unix_timestamp >= escrow.refund_after,
        EscrowError::RefundTooEarly
    );
    let amount = escrow
        .total
        .checked_sub(escrow.released)
        .ok_or(EscrowError::MathOverflow)?;
    require!(amount > 0, EscrowError::NothingToRefund);

    // Effects.
    for milestone in escrow.milestones.iter_mut() {
        if milestone.status == MilestoneStatus::Pending {
            milestone.status = MilestoneStatus::Cancelled;
        }
    }
    escrow.status = EscrowStatus::Refunded;

    // Interaction: the vault's authority is the escrow PDA.
    let event = PoRefunded {
        escrow: escrow_key,
        chain_po_id: escrow.chain_po_id.clone(),
        amount,
        to: ctx.accounts.buyer_token.key(),
        slot: clock.slot,
    };
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
                to: ctx.accounts.buyer_token.to_account_info(),
                authority: escrow.to_account_info(),
            },
            &[seeds],
        ),
        amount,
        ctx.accounts.mint.decimals,
    )?;

    emit_cpi!(event);
    Ok(())
}
