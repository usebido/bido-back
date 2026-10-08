//! Fixed-offset parsers for Solana Attestation Service accounts (spec 01 sections 3.5 and 3.6).
//!
//! SAS lays each account out as a one-byte discriminator, then fixed-size fields, then byte
//! strings and lists prefixed with a u32 LE length. A parser returns `None` for data that is not a
//! well-formed account of its kind, so each caller maps the failure to its own error.

use anchor_lang::prelude::*;

use crate::{
    constants::{GATE_SCHEMA_LAYOUT, SAS_CREDENTIAL_DISC, SAS_PROGRAM_ID, SAS_SCHEMA_DISC},
    errors::EscrowError,
};

/// A SAS `Credential`: `[0] ‖ authority (1..33) ‖ u32 len ‖ name ‖ u32 count ‖ count × signer`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Credential {
    pub authority: Pubkey,
    pub authorized_signers: Vec<Pubkey>,
}

impl Credential {
    pub fn parse(data: &[u8]) -> Option<Self> {
        let mut reader = Reader::new(data);
        if reader.u8()? != SAS_CREDENTIAL_DISC {
            return None;
        }
        let authority = reader.key()?;
        reader.bytes()?; // name
        let count = reader.u32()? as usize;
        let signers = reader.take(count.checked_mul(32)?)?;
        let authorized_signers = signers
            .chunks_exact(32)
            .map(|key| Pubkey::try_from(key).ok())
            .collect::<Option<Vec<_>>>()?;
        Some(Self {
            authority,
            authorized_signers,
        })
    }
}

/// A SAS `Schema`: `[1] ‖ credential (1..33) ‖ name ‖ description ‖ layout ‖ field_names ‖
/// is_paused (u8) ‖ version (u8)`, where each of the four byte strings is `u32 len ‖ bytes`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Schema<'a> {
    pub credential: Pubkey,
    /// One SAS type id per field; Bido's gate schema is `GATE_SCHEMA_LAYOUT`.
    pub layout: &'a [u8],
    pub is_paused: bool,
    pub version: u8,
}

impl<'a> Schema<'a> {
    pub fn parse(data: &'a [u8]) -> Option<Self> {
        let mut reader = Reader::new(data);
        if reader.u8()? != SAS_SCHEMA_DISC {
            return None;
        }
        let credential = reader.key()?;
        reader.bytes()?; // name
        reader.bytes()?; // description
        let layout = reader.bytes()?;
        reader.bytes()?; // field_names
        let is_paused = reader.u8()? != 0;
        let version = reader.u8()?;
        Some(Self {
            credential,
            layout,
            is_paused,
            version,
        })
    }
}

/// The authorized signers of `credential`, if it is a SAS-owned `Credential` account
/// (`create_po` check 10).
pub fn parse_credential_signers(credential: &AccountInfo) -> Option<Vec<Pubkey>> {
    if *credential.owner != SAS_PROGRAM_ID {
        return None;
    }
    let data = credential.try_borrow_data().ok()?;
    Credential::parse(&data).map(|parsed| parsed.authorized_signers)
}

/// SAS config checks of `init_config` and `update_config` (spec 01 section 3.5): `credential` is a
/// SAS credential and `schema` an unpaused SAS schema of that credential with the gate layout.
/// Every failure is `InvalidSasConfig`.
pub fn check_sas_config(credential: &AccountInfo, schema: &AccountInfo) -> Result<()> {
    require_keys_eq!(
        *credential.owner,
        SAS_PROGRAM_ID,
        EscrowError::InvalidSasConfig
    );
    require!(
        Credential::parse(&credential.try_borrow_data()?).is_some(),
        EscrowError::InvalidSasConfig
    );
    require_keys_eq!(*schema.owner, SAS_PROGRAM_ID, EscrowError::InvalidSasConfig);
    let schema_data = schema.try_borrow_data()?;
    let parsed = Schema::parse(&schema_data).ok_or(EscrowError::InvalidSasConfig)?;
    require_keys_eq!(
        parsed.credential,
        credential.key(),
        EscrowError::InvalidSasConfig
    );
    require!(
        parsed.layout == GATE_SCHEMA_LAYOUT.as_slice(),
        EscrowError::InvalidSasConfig
    );
    require!(!parsed.is_paused, EscrowError::InvalidSasConfig);
    Ok(())
}

/// Reads an account's fields in order, failing on any read past the end.
struct Reader<'a> {
    data: &'a [u8],
    at: usize,
}

impl<'a> Reader<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self { data, at: 0 }
    }

    fn take(&mut self, len: usize) -> Option<&'a [u8]> {
        let end = self.at.checked_add(len)?;
        let bytes = self.data.get(self.at..end)?;
        self.at = end;
        Some(bytes)
    }

    fn u8(&mut self) -> Option<u8> {
        self.take(1).map(|bytes| bytes[0])
    }

    fn u32(&mut self) -> Option<u32> {
        self.take(4)?.try_into().ok().map(u32::from_le_bytes)
    }

    fn key(&mut self) -> Option<Pubkey> {
        Pubkey::try_from(self.take(32)?).ok()
    }

    /// A u32-LE-length-prefixed byte string.
    fn bytes(&mut self) -> Option<&'a [u8]> {
        let len = self.u32()? as usize;
        self.take(len)
    }
}
