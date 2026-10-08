// Chain constants shared by the instruction builders and devnet setup.
import * as generated from "./constants";
import type { BidoEscrow } from "../../idl/bido_escrow";

/** Solana Attestation Service, devnet. */
export const SAS_PROGRAM_ID = generated.SAS_PROGRAM_ID;
export const SAS_CREDENTIAL_NAME = generated.SAS_CREDENTIAL_NAME;
export const SAS_SCHEMA_NAME = generated.SAS_SCHEMA_NAME;
export const SAS_SCHEMA_VERSION = generated.SAS_SCHEMA_VERSION;
/** VecU8 po_hash, U8 gate, VecU8 evidence_hash. */
export const SAS_SCHEMA_LAYOUT = generated.SAS_SCHEMA_LAYOUT;
export const SAS_SCHEMA_FIELDS = generated.SAS_SCHEMA_FIELDS;

/** Domain prefix of the gate nonce: `sha256("bido-gate-v1" ‖ escrow ‖ u8 gate)`. */
export const GATE_NONCE_DOMAIN = generated.GATE_NONCE_DOMAIN;
export const MAX_MILESTONES = generated.MAX_MILESTONES;
export const MAX_GATES = generated.MAX_GATES;
/** Bytes; `chain_po_id` is a raw PDA seed. */
export const MAX_CHAIN_PO_ID_LEN = generated.MAX_CHAIN_PO_ID_LEN;
/** Bytes of a milestone name. */
export const MAX_MILESTONE_NAME_LEN = generated.MAX_MILESTONE_NAME_LEN;
/** `refund_after = now + 90 days`, set by the `create_po` builder. */
export const ESCROW_REFUND_AFTER_SECONDS = generated.ESCROW_REFUND_AFTER_SECONDS;

/**
 * Compute-unit limit per transaction part (§3.9): `approval` covers CreateAttestation plus up to
 * two releases, `release` is per release instruction. Raised to 1.3 × the measured maximum once
 * `deployments/devnet.json.measurements` has numbers.
 */
export const CU_LIMITS = generated.CU_LIMITS;
/** Largest serialized transaction the cluster accepts. */
export const MAX_TX_BYTES = generated.MAX_TX_BYTES;

export const CIRCLE_DEVNET_USDC_MINT = generated.CIRCLE_DEVNET_USDC_MINT;
/** SPL Memo v2. */
export const MEMO_PROGRAM_ID = generated.MEMO_PROGRAM_ID;

/**
 * `EscrowError` variant names. The generated `BidoEscrow` type camelCases them; capitalising gives
 * back the Anchor names, so a renamed or appended IDL error breaks the records below at typecheck.
 */
export type ProgramErrorName = Capitalize<BidoEscrow["errors"][number]["name"]>;

/** Anchor error codes (offset 6000, §3.7 order: append only, never reorder). */
export const PROGRAM_ERROR_CODES: Record<ProgramErrorName, number> = {
  UnauthorizedAdmin: 6000,
  UnauthorizedOperator: 6001,
  UnauthorizedBuyer: 6002,
  InvalidSasConfig: 6003,
  MintNotAllowed: 6004,
  InvalidPoId: 6005,
  InvalidMilestoneCount: 6006,
  NameTooLong: 6007,
  ZeroBps: 6008,
  BpsSumNot10000: 6009,
  ZeroAmount: 6010,
  MathOverflow: 6011,
  InvalidGate: 6012,
  InvalidAttester: 6013,
  GateAttesterMismatch: 6014,
  PayoutIsEscrow: 6015,
  PayoutIsBuyer: 6016,
  VaultAccountInvalid: 6017,
  InvalidRefundDeadline: 6018,
  AlreadyFunded: 6019,
  NotFunded: 6020,
  EscrowClosed: 6021,
  FundDeltaMismatch: 6022,
  InvalidMilestone: 6023,
  MilestoneAlreadyReleased: 6024,
  AttestationAddressMismatch: 6025,
  AttestationMissing: 6026,
  AttestationMalformed: 6027,
  AttestationCredentialMismatch: 6028,
  AttestationSchemaMismatch: 6029,
  AttestationNonceMismatch: 6030,
  AttestationPoHashMismatch: 6031,
  AttestationGateMismatch: 6032,
  AttesterMismatch: 6033,
  AttestationExpired: 6034,
  PayoutMismatch: 6035,
  RefundTooEarly: 6036,
  NothingToRefund: 6037,
};
