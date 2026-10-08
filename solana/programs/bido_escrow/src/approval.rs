//! The attestation verifier (spec 01 section 3.6): the only code that reads a SAS attestation. The
//! fallback of spec 01 section 6 replaces only this function and its account.

use anchor_lang::prelude::*;

use crate::{
    constants::{ATTESTATION_ACCOUNT_LEN, GATE_DATA_LEN, SAS_ATTESTATION_DISC, SAS_PROGRAM_ID},
    errors::EscrowError,
    instructions::create_po::gate_nonce,
    state::PoEscrow,
};

// Byte offsets in a SAS attestation of the gate schema (246 bytes; spec 01 section 3.6).
const NONCE: usize = 1;
const CREDENTIAL: usize = 33;
const SCHEMA: usize = 65;
const DATA_LEN: usize = 97;
const PO_HASH_LEN: usize = 101;
const PO_HASH: usize = 105;
const GATE: usize = 137;
const EVIDENCE_HASH_LEN: usize = 138;
const EVIDENCE_HASH: usize = 142;
const SIGNER: usize = 174;
const EXPIRY: usize = 206;
const TOKEN_ACCOUNT: usize = 214;
/// Both gate data fields are 32-byte hashes.
const HASH_LEN: u32 = 32;

/// What a valid approval contributes to the released milestone.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ApprovalInfo {
    /// The attestation signer, equal to the gate's pinned attester.
    pub signer: Pubkey,
    pub evidence_hash: [u8; 32],
}

/// Checks 4-13 of `release` (spec 01 section 3.6), in order, each with its own error: `attestation`
/// is the SAS attestation at `gate`'s expected address, well formed, under the escrow's credential
/// and schema, for this escrow, gate and PO document, signed by the gate's attester and unexpired.
pub fn verify_approval(
    escrow: &PoEscrow,
    escrow_key: &Pubkey,
    gate: u8,
    attestation: &AccountInfo,
    now: i64,
) -> Result<ApprovalInfo> {
    // 4. Defensive: `create_po` only stores milestones whose gate exists.
    let pinned = escrow
        .gates
        .get(usize::from(gate))
        .ok_or(EscrowError::InvalidGate)?;

    // 5. The address encodes credential, schema, escrow and gate.
    require_keys_eq!(
        attestation.key(),
        pinned.attestation,
        EscrowError::AttestationAddressMismatch
    );

    // 6. A never-created or closed account is System-owned; a forgery has another owner.
    require_keys_eq!(
        *attestation.owner,
        SAS_PROGRAM_ID,
        EscrowError::AttestationMissing
    );

    // 7.
    let data = attestation.try_borrow_data()?;
    require!(
        data.len() == ATTESTATION_ACCOUNT_LEN
            && data[0] == SAS_ATTESTATION_DISC
            && u32_at(&data, DATA_LEN) == GATE_DATA_LEN as u32
            && u32_at(&data, PO_HASH_LEN) == HASH_LEN
            && u32_at(&data, EVIDENCE_HASH_LEN) == HASH_LEN
            && key_at(&data, TOKEN_ACCOUNT) == Pubkey::default(),
        EscrowError::AttestationMalformed
    );

    // 8.
    require_keys_eq!(
        key_at(&data, CREDENTIAL),
        escrow.credential,
        EscrowError::AttestationCredentialMismatch
    );
    require_keys_eq!(
        key_at(&data, SCHEMA),
        escrow.schema,
        EscrowError::AttestationSchemaMismatch
    );

    // 9. Recomputed, as defence in depth behind check 5.
    require_keys_eq!(
        key_at(&data, NONCE),
        gate_nonce(escrow_key, gate),
        EscrowError::AttestationNonceMismatch
    );

    // 10.
    require!(
        hash_at(&data, PO_HASH) == escrow.po_hash,
        EscrowError::AttestationPoHashMismatch
    );

    // 11.
    require!(data[GATE] == gate, EscrowError::AttestationGateMismatch);

    // 12. The pin set at `create_po` and approved by the buyer, not the credential's signer list.
    let signer = key_at(&data, SIGNER);
    require_keys_eq!(signer, pinned.attester, EscrowError::AttesterMismatch);

    // 13. Bido always writes 0 (never expires).
    let expiry = i64::from_le_bytes(array_at(&data, EXPIRY));
    require!(expiry == 0 || expiry > now, EscrowError::AttestationExpired);

    Ok(ApprovalInfo {
        signer,
        evidence_hash: hash_at(&data, EVIDENCE_HASH),
    })
}

/// `N` bytes at `at`; callers read only inside the length checked by check 7.
fn array_at<const N: usize>(data: &[u8], at: usize) -> [u8; N] {
    let mut out = [0; N];
    out.copy_from_slice(&data[at..at + N]);
    out
}

fn u32_at(data: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(array_at(data, at))
}

fn hash_at(data: &[u8], at: usize) -> [u8; 32] {
    array_at(data, at)
}

fn key_at(data: &[u8], at: usize) -> Pubkey {
    Pubkey::new_from_array(array_at(data, at))
}
