//! Program accounts (spec 01 section 3.4).

use anchor_lang::prelude::*;

use crate::constants::{MAX_CHAIN_PO_ID_LEN, MAX_GATES, MAX_MILESTONES, MAX_MILESTONE_NAME_LEN};

/// Program-wide settings. PDA `["config"]`.
#[account]
#[derive(InitSpace)]
pub struct Config {
    /// Program upgrade authority at the last `init_config` or `update_config`.
    pub admin: Pubkey,
    /// The only key allowed to call `create_po`.
    pub operator: Pubkey,
    /// `[tUSD, Circle devnet USDC]`; `Pubkey::default()` marks an empty slot.
    pub allowed_mints: [Pubkey; 2],
    /// SAS credential "bido-approvals".
    pub sas_credential: Pubkey,
    /// SAS schema "bido-gate-approval" v1.
    pub sas_schema: Pubkey,
    pub bump: u8,
}

/// One purchase order's escrow and audit record. PDA `["po", buyer, chain_po_id]`; never closed.
#[account]
#[derive(InitSpace)]
pub struct PoEscrow {
    #[max_len(MAX_CHAIN_PO_ID_LEN)]
    pub chain_po_id: String,
    /// Signs `fund` and `refund` (the buyer approver's passkey wallet).
    pub buyer: Pubkey,
    /// Payout owner wallet from the verified registry; immutable.
    pub payout: Pubkey,
    /// ATA(payout, mint), created at `create_po`.
    pub payout_token: Pubkey,
    pub mint: Pubkey,
    /// ATA(escrow, mint); its authority is this PDA.
    pub vault: Pubkey,
    /// sha256 of the PO PDF bytes.
    pub po_hash: [u8; 32],
    /// sha256 of the drawing 2D PDF (rev C).
    pub cad_hash: [u8; 32],
    /// Base units (6 decimals).
    pub total: u64,
    pub released: u64,
    pub status: EscrowStatus,
    /// SAS credential copied from `Config` at creation.
    pub credential: Pubkey,
    /// SAS schema copied from `Config` at creation.
    pub schema: Pubkey,
    #[max_len(MAX_GATES)]
    pub gates: Vec<Gate>,
    #[max_len(MAX_MILESTONES)]
    pub milestones: Vec<Milestone>,
    /// Unix seconds after which the buyer may refund.
    pub refund_after: i64,
    pub bump: u8,
}

/// An approval point with one pinned attester.
#[derive(AnchorSerialize, AnchorDeserialize, Clone, Debug, PartialEq, Eq, InitSpace)]
pub struct Gate {
    pub attester: Pubkey,
    /// Expected SAS attestation PDA `["attestation", credential, schema, gate nonce]`.
    pub attestation: Pubkey,
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Debug, PartialEq, Eq, InitSpace)]
pub struct Milestone {
    #[max_len(MAX_MILESTONE_NAME_LEN)]
    pub name: String,
    pub bps: u16,
    /// Fixed at `create_po`.
    pub amount: u64,
    pub gate: u8,
    pub status: MilestoneStatus,
    /// 0 until released.
    pub released_slot: u64,
    /// Attestation signer; default until released.
    pub approved_by: Pubkey,
    /// From the attestation; zero until released.
    pub evidence_hash: [u8; 32],
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, Debug, PartialEq, Eq, InitSpace)]
pub enum EscrowStatus {
    Created,
    Funded,
    Completed,
    Refunded,
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, Debug, PartialEq, Eq, InitSpace)]
pub enum MilestoneStatus {
    Pending,
    Released,
    Cancelled,
}
