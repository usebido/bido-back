//! Program events (spec 01 section 3.8), emitted with `emit_cpi!` through the event authority
//! PDA `["__event_authority"]`, so indexers read them from inner instructions.

use anchor_lang::prelude::*;

#[event]
pub struct PoCreated {
    pub escrow: Pubkey,
    pub chain_po_id: String,
    pub buyer: Pubkey,
    pub payout: Pubkey,
    pub payout_token: Pubkey,
    pub mint: Pubkey,
    pub vault: Pubkey,
    pub po_hash: [u8; 32],
    pub cad_hash: [u8; 32],
    pub total: u64,
    pub credential: Pubkey,
    pub schema: Pubkey,
    pub gate_attesters: Vec<Pubkey>,
    pub gate_attestations: Vec<Pubkey>,
    pub milestone_names: Vec<String>,
    pub milestone_bps: Vec<u16>,
    pub milestone_gates: Vec<u8>,
    pub milestone_amounts: Vec<u64>,
    pub refund_after: i64,
    pub slot: u64,
}

#[event]
pub struct PoFunded {
    pub escrow: Pubkey,
    pub chain_po_id: String,
    pub buyer: Pubkey,
    pub amount: u64,
    pub vault: Pubkey,
    pub slot: u64,
}

#[event]
pub struct MilestoneReleased {
    pub escrow: Pubkey,
    pub chain_po_id: String,
    pub milestone: u8,
    pub gate: u8,
    pub amount: u64,
    pub released_total: u64,
    pub payout: Pubkey,
    pub payout_token: Pubkey,
    pub attestation: Pubkey,
    pub approved_by: Pubkey,
    pub evidence_hash: [u8; 32],
    pub slot: u64,
}

#[event]
pub struct PoCompleted {
    pub escrow: Pubkey,
    pub chain_po_id: String,
    pub total: u64,
    pub slot: u64,
}

#[event]
pub struct PoRefunded {
    pub escrow: Pubkey,
    pub chain_po_id: String,
    pub amount: u64,
    pub to: Pubkey,
    pub slot: u64,
}
