use anchor_lang::prelude::*;

use crate::{
    constants::CONFIG_SEED, errors::EscrowError, program::BidoEscrow, sas::check_sas_config,
    state::Config,
};

/// `Some` fields replace the stored values; `None` keeps them.
#[derive(AnchorSerialize, AnchorDeserialize, Clone, Debug, PartialEq, Eq)]
pub struct UpdateConfigArgs {
    pub operator: Option<Pubkey>,
    pub allowed_mints: Option<[Pubkey; 2]>,
    pub sas_credential: Option<Pubkey>,
    pub sas_schema: Option<Pubkey>,
}

#[derive(Accounts)]
pub struct UpdateConfig<'info> {
    pub admin: Signer<'info>,
    #[account(mut, seeds = [CONFIG_SEED], bump = config.bump)]
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
    /// CHECK: the resulting SAS credential; the handler re-runs the SAS config checks on it.
    pub sas_credential: UncheckedAccount<'info>,
    /// CHECK: the resulting SAS schema; the handler re-runs the SAS config checks on it.
    pub sas_schema: UncheckedAccount<'info>,
}

/// The account constraints already checked the current upgrade authority (`UnauthorizedAdmin`),
/// not the stored `config.admin`, so a rotated key works and the old one does not.
pub fn handle_update_config(ctx: Context<UpdateConfig>, args: UpdateConfigArgs) -> Result<()> {
    let accounts = ctx.accounts;
    let config = &mut accounts.config;
    config.admin = accounts.admin.key();
    if let Some(operator) = args.operator {
        config.operator = operator;
    }
    if let Some(allowed_mints) = args.allowed_mints {
        config.allowed_mints = allowed_mints;
    }
    if let Some(sas_credential) = args.sas_credential {
        config.sas_credential = sas_credential;
    }
    if let Some(sas_schema) = args.sas_schema {
        config.sas_schema = sas_schema;
    }

    // The SAS accounts passed must be the resulting pair, which must still pass every check.
    require_keys_eq!(
        accounts.sas_credential.key(),
        config.sas_credential,
        EscrowError::InvalidSasConfig
    );
    require_keys_eq!(
        accounts.sas_schema.key(),
        config.sas_schema,
        EscrowError::InvalidSasConfig
    );
    check_sas_config(&accounts.sas_credential, &accounts.sas_schema)
}
