//! Program errors (spec 01 section 3.7), codes from 6000.
//!
//! The order is frozen: append only, never reorder or remove (the web maps names through
//! `PROGRAM_ERROR_COPY`). Messages appear in public explorer logs.

use anchor_lang::prelude::*;

#[error_code]
pub enum EscrowError {
    #[msg("Signer is not the program upgrade authority")]
    UnauthorizedAdmin, // 6000
    #[msg("Signer is not the configured operator")]
    UnauthorizedOperator, // 6001
    #[msg("Signer is not this escrow's buyer")]
    UnauthorizedBuyer, // 6002
    #[msg("SAS credential or schema is not valid for Bido gate approvals")]
    InvalidSasConfig, // 6003
    #[msg("Mint is not allowed or does not match the escrow")]
    MintNotAllowed, // 6004
    #[msg("Invalid on-chain PO id")]
    InvalidPoId, // 6005
    #[msg("A PO needs 1 to 4 milestones")]
    InvalidMilestoneCount, // 6006
    #[msg("Milestone name must be 1 to 24 bytes")]
    NameTooLong, // 6007
    #[msg("Milestone share must be greater than zero")]
    ZeroBps, // 6008
    #[msg("Milestone shares must add up to 100%")]
    BpsSumNot10000, // 6009
    #[msg("Total and every milestone amount must be greater than zero")]
    ZeroAmount, // 6010
    #[msg("Arithmetic overflow")]
    MathOverflow, // 6011
    #[msg("Invalid or unused approval gate")]
    InvalidGate, // 6012
    #[msg("Gate attester is empty or equals the payout")]
    InvalidAttester, // 6013
    #[msg("Gate attester is not an authorized signer of the credential")]
    GateAttesterMismatch, // 6014
    #[msg("Payout cannot be the escrow or its vault")]
    PayoutIsEscrow, // 6015
    #[msg("Payout cannot be the buyer")]
    PayoutIsBuyer, // 6016
    #[msg("Vault token account is not the escrow's clean vault")]
    VaultAccountInvalid, // 6017
    #[msg("Refund deadline must be in the future")]
    InvalidRefundDeadline, // 6018
    #[msg("Escrow is already funded")]
    AlreadyFunded, // 6019
    #[msg("Escrow is not funded")]
    NotFunded, // 6020
    #[msg("Escrow is completed or refunded")]
    EscrowClosed, // 6021
    #[msg("Vault did not receive exactly the PO total")]
    FundDeltaMismatch, // 6022
    #[msg("Milestone index out of range")]
    InvalidMilestone, // 6023
    #[msg("Milestone already released")]
    MilestoneAlreadyReleased, // 6024
    #[msg("Attestation does not belong to this escrow and gate")]
    AttestationAddressMismatch, // 6025
    #[msg("No approval attestation exists for this gate")]
    AttestationMissing, // 6026
    #[msg("Attestation account is malformed")]
    AttestationMalformed, // 6027
    #[msg("Attestation credential mismatch")]
    AttestationCredentialMismatch, // 6028
    #[msg("Attestation schema mismatch")]
    AttestationSchemaMismatch, // 6029
    #[msg("Attestation nonce mismatch")]
    AttestationNonceMismatch, // 6030
    #[msg("Attestation is for a different PO document")]
    AttestationPoHashMismatch, // 6031
    #[msg("Attestation is for a different gate")]
    AttestationGateMismatch, // 6032
    #[msg("Attestation was not signed by this gate's approver")]
    AttesterMismatch, // 6033
    #[msg("Attestation has expired")]
    AttestationExpired, // 6034
    #[msg("Destination is not the escrow's fixed payout account")]
    PayoutMismatch, // 6035
    #[msg("Refund deadline has not passed")]
    RefundTooEarly, // 6036
    #[msg("Nothing left to refund")]
    NothingToRefund, // 6037
}
