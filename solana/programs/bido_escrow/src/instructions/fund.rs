use anchor_lang::prelude::*;
use anchor_spl::token_interface::{
    transfer_checked, Mint, TokenAccount, TokenInterface, TransferChecked,
};

use crate::{
    errors::EscrowError,
    events::PoFunded,
    state::{EscrowStatus, PoEscrow},
};

#[event_cpi]
#[derive(Accounts)]
pub struct Fund<'info> {
    #[account(address = escrow.buyer @ EscrowError::UnauthorizedBuyer)]
    pub buyer: Signer<'info>,
    #[account(mut)]
    pub escrow: Box<Account<'info, PoEscrow>>,
    #[account(
        mut,
        token::mint = escrow.mint,
        token::authority = buyer,
    )]
    pub buyer_token: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(mut, address = escrow.vault @ EscrowError::VaultAccountInvalid)]
    pub vault: Box<InterfaceAccount<'info, TokenAccount>>,
    #[account(address = escrow.mint @ EscrowError::MintNotAllowed)]
    pub mint: Box<InterfaceAccount<'info, Mint>>,
    pub token_program: Interface<'info, TokenInterface>,
}

/// Moves exactly `total` from the buyer into the vault (the buyer check is the account
/// constraint on `buyer`). The vault may already hold a donation, so only the delta counts.
pub fn handle_fund(ctx: Context<Fund>) -> Result<()> {
    match ctx.accounts.escrow.status {
        EscrowStatus::Created => {}
        EscrowStatus::Funded => return err!(EscrowError::AlreadyFunded),
        EscrowStatus::Completed | EscrowStatus::Refunded => return err!(EscrowError::EscrowClosed),
    }

    let total = ctx.accounts.escrow.total;
    let pre = ctx.accounts.vault.amount;
    transfer_checked(
        CpiContext::new(
            ctx.accounts.token_program.key(),
            TransferChecked {
                from: ctx.accounts.buyer_token.to_account_info(),
                mint: ctx.accounts.mint.to_account_info(),
                to: ctx.accounts.vault.to_account_info(),
                authority: ctx.accounts.buyer.to_account_info(),
            },
        ),
        total,
        ctx.accounts.mint.decimals,
    )?;
    ctx.accounts.vault.reload()?;
    let delta = ctx
        .accounts
        .vault
        .amount
        .checked_sub(pre)
        .ok_or(EscrowError::FundDeltaMismatch)?;
    require!(delta == total, EscrowError::FundDeltaMismatch);

    let escrow = &mut ctx.accounts.escrow;
    escrow.status = EscrowStatus::Funded;
    let event = PoFunded {
        escrow: escrow.key(),
        chain_po_id: escrow.chain_po_id.clone(),
        buyer: escrow.buyer,
        amount: total,
        vault: escrow.vault,
        slot: Clock::get()?.slot,
    };
    emit_cpi!(event);
    Ok(())
}
