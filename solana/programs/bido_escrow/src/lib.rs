//! `bido_escrow` holds a purchase order's test dollars on Solana devnet and pays the factory's
//! fixed payout one milestone at a time, only against a Solana Attestation Service (SAS)
//! attestation signed by the approver pinned for that milestone's gate.
//!
//! Design: `docs/specs/01-escrow-program.md`. These six instructions are the whole interface: no
//! close, payout update, attester update or admin withdrawal exists.

use anchor_lang::prelude::*;

pub mod approval;
pub mod constants;
pub mod errors;
pub mod events;
pub mod instructions;
pub mod sas;
pub mod state;

pub use constants::*;
pub use errors::*;
pub use events::*;
pub use instructions::*;
pub use state::*;

declare_id!("7992GKVq51ChvsVJQ9GFto2aBozUGuMcuY93SUSxexJw");

#[program]
pub mod bido_escrow {
    use super::*;

    /// One-shot setup of `Config`, signed by the program upgrade authority.
    pub fn init_config(ctx: Context<InitConfig>, args: InitConfigArgs) -> Result<()> {
        instructions::init_config::handle_init_config(ctx, args)
    }

    /// Updates `Config`, signed by the current upgrade authority; never touches a `PoEscrow`.
    pub fn update_config(ctx: Context<UpdateConfig>, args: UpdateConfigArgs) -> Result<()> {
        instructions::update_config::handle_update_config(ctx, args)
    }

    /// Creates an unfunded escrow for a PO, signed by the operator; the relayer pays.
    pub fn create_po(ctx: Context<CreatePo>, args: CreatePoArgs) -> Result<()> {
        instructions::create_po::handle_create_po(ctx, args)
    }

    /// Moves exactly the PO total from the buyer into the vault, signed by the buyer.
    pub fn fund(ctx: Context<Fund>) -> Result<()> {
        instructions::fund::handle_fund(ctx)
    }

    /// Pays one milestone to the fixed payout against its gate's SAS attestation. Permissionless.
    pub fn release(ctx: Context<Release>, milestone: u8) -> Result<()> {
        instructions::release::handle_release(ctx, milestone)
    }

    /// Returns the unreleased balance to the buyer's ATA after `refund_after`, signed by the buyer.
    pub fn refund(ctx: Context<Refund>) -> Result<()> {
        instructions::refund::handle_refund(ctx)
    }
}
