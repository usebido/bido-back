//! Program constants (spec 01 section 3.3).

use anchor_lang::prelude::*;

/// Solana Attestation Service program.
pub const SAS_PROGRAM_ID: Pubkey = pubkey!("22zoJMtdu4tQc2PzL74ZUT7FrwgB1Udec8DdW4yw4BdG");
pub const CONFIG_SEED: &[u8] = b"config";
pub const PO_SEED: &[u8] = b"po";
pub const GATE_NONCE_DOMAIN: &[u8] = b"bido-gate-v1";
pub const MAX_CHAIN_PO_ID_LEN: usize = 32; // used raw as a PDA seed
pub const MAX_MILESTONE_NAME_LEN: usize = 24;
pub const MAX_MILESTONES: usize = 4; // demo uses 3
pub const MAX_GATES: usize = 4; // demo uses 2
pub const BPS_TOTAL: u16 = 10_000;
pub const SAS_CREDENTIAL_DISC: u8 = 0;
pub const SAS_SCHEMA_DISC: u8 = 1;
pub const SAS_ATTESTATION_DISC: u8 = 2;
pub const GATE_SCHEMA_LAYOUT: [u8; 3] = [13, 0, 13]; // VecU8 po_hash, U8 gate, VecU8 evidence_hash
pub const GATE_DATA_LEN: usize = 4 + 32 + 1 + 4 + 32; // 73
pub const ATTESTATION_ACCOUNT_LEN: usize = 1 + 32 + 32 + 32 + 4 + GATE_DATA_LEN + 32 + 8 + 32; // 246

const _: () = assert!(GATE_DATA_LEN == 73);
const _: () = assert!(ATTESTATION_ACCOUNT_LEN == 246);
