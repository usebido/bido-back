use anchor_lang::prelude::*;

use crate::{
    constants::CONFIG_SEED, errors::EscrowError, program::BidoEscrow, sas::check_sas_config,
    state::Config,
};

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Debug, PartialEq, Eq)]
pub struct InitConfigArgs {
    pub operator: Pubkey,
    pub allowed_mints: [Pubkey; 2],
}

#[derive(Accounts)]
pub struct InitConfig<'info> {
    #[account(mut)]
    pub admin: Signer<'info>,
    #[account(
        init,
        payer = admin,
        space = Config::DISCRIMINATOR.len() + Config::INIT_SPACE,
        seeds = [CONFIG_SEED],
        bump,
    )]
    pub config: Account<'info, Config>,
    #[account(
        constraint = program.programdata_address()? == Some(program_data.key())
            @ EscrowError::UnauthorizedAdmin,
    )]
    pub program: Program<'info, BidoEscrow>,
    #[account(
        constraint = program_data.upgrade_authority_address == Some(admin.key())
            @ EscrowError::UnauthorizedAdmin,
    )]
    pub program_data: Account<'info, ProgramData>,
    /// CHECK: SAS credential; the handler checks owner, discriminator and fields (SAS config checks).
    pub sas_credential: UncheckedAccount<'info>,
    /// CHECK: SAS schema; the handler checks owner, discriminator, credential, layout and pause flag.
    pub sas_schema: UncheckedAccount<'info>,
    pub system_program: Program<'info, System>,
}

/// The account constraints already checked the upgrade authority (`UnauthorizedAdmin`).
pub fn handle_init_config(ctx: Context<InitConfig>, args: InitConfigArgs) -> Result<()> {
    check_sas_config(&ctx.accounts.sas_credential, &ctx.accounts.sas_schema)?;
    require_keys_neq!(args.operator, Pubkey::default());
    require_keys_neq!(args.allowed_mints[0], Pubkey::default());

    ctx.accounts.config.set_inner(Config {
        admin: ctx.accounts.admin.key(),
        operator: args.operator,
        allowed_mints: args.allowed_mints,
        sas_credential: ctx.accounts.sas_credential.key(),
        sas_schema: ctx.accounts.sas_schema.key(),
        bump: ctx.bumps.config,
    });
    Ok(())
}
