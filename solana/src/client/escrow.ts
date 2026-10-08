// The escrow program's accounts and instructions (spec 01 §3.4, §3.5, §3.11). `escrowCoder` is
// Anchor's `BorshCoder` on the synced IDL (snake_case names, as the IDL has them); the decoders
// return the camelCase types of §3.4 with addresses as base58, hashes as lowercase hex and u64/i64
// as `bigint`, as `sas.ts` does; each builder returns one program instruction, accounts in IDL
// order with the IDL's signer and writable flags. Twin of `bido_back.chain.codec.escrow`. Callers
// pass the program id; nothing here reads env, keys or the network.

import { BN, BorshCoder, type Idl } from "@anchor-lang/core";
import { getAssociatedTokenAddressSync, TOKEN_PROGRAM_ID } from "@solana/spl-token";
import { PublicKey, TransactionInstruction, type AccountMeta } from "@solana/web3.js";

import { MEMO_PROGRAM_ID, type AppErrorCode } from "../contracts";
import idl from "../../idl/bido_escrow.json";

import { PAYOUT_REF_MEMO_PREFIX } from "./hashes";
import { configPda, escrowPda, eventAuthorityPda, payoutAta, vaultAta } from "./pdas";

const IDL = idl as Idl;

/** Encodes and decodes `bido_escrow` instructions, accounts and events from the synced IDL. */
export const escrowCoder = new BorshCoder(IDL);

const MEMO_PROGRAM = new PublicKey(MEMO_PROGRAM_ID);
const HASH_LEN = 32;
const SHA256_HEX_RE = /^[0-9a-f]{64}$/;

const ESCROW_STATUSES = ["Created", "Funded", "Completed", "Refunded"] as const;
const MILESTONE_STATUSES = ["Pending", "Released", "Cancelled"] as const;

export type EscrowStatus = (typeof ESCROW_STATUSES)[number];
export type MilestoneStatus = (typeof MILESTONE_STATUSES)[number];

export interface Gate {
  attester: string;
  /** The expected SAS attestation PDA `["attestation", credential, schema, gate nonce]`. */
  attestation: string;
}

export interface Milestone {
  name: string;
  bps: number;
  amount: bigint;
  gate: number;
  status: MilestoneStatus;
  /** 0 until released. */
  releasedSlot: bigint;
  /** The attestation signer; the default address until released. */
  approvedBy: string;
  /** From the attestation; zero until released. */
  evidenceHash: string;
}

/** A decoded `PoEscrow` (PDA `["po", buyer, chain_po_id]`); amounts in base units. */
export interface PoEscrowAccount {
  chainPoId: string;
  buyer: string;
  payout: string;
  payoutToken: string;
  mint: string;
  vault: string;
  poHash: string;
  cadHash: string;
  total: bigint;
  released: bigint;
  status: EscrowStatus;
  credential: string;
  schema: string;
  gates: Gate[];
  milestones: Milestone[];
  /** Unix seconds. */
  refundAfter: bigint;
  bump: number;
}

/** A decoded `Config` (PDA `["config"]`); the default address marks an empty mint slot. */
export interface ConfigAccount {
  admin: string;
  operator: string;
  allowedMints: [string, string];
  sasCredential: string;
  sasSchema: string;
  bump: number;
}

export interface MilestoneInput {
  name: string;
  bps: number;
  gate: number;
}

/** `create_po` arguments; `refundAfter` is `now + ESCROW_REFUND_AFTER_SECONDS` (unix seconds). */
export interface CreatePoArgs {
  chainPoId: string;
  /** sha256 of the PO PDF: 32 bytes or 64 lowercase hex digits. */
  poHash: Uint8Array | string;
  /** sha256 of the drawing: 32 bytes or 64 lowercase hex digits. */
  cadHash: Uint8Array | string;
  /** Base units. */
  total: bigint;
  gateAttesters: PublicKey[];
  milestones: MilestoneInput[];
  refundAfter: bigint;
}

/** A transaction that cannot be built from the given state, with its app error code. */
export class ChainBuildError extends Error {
  readonly code: AppErrorCode;

  constructor(code: AppErrorCode, message: string = code) {
    super(message);
    this.name = "ChainBuildError";
    this.code = code;
  }
}

// ---- decoders ----

/** What Anchor's coder returns: BN integers, `PublicKey`s, byte arrays and `{ Variant: {} }`. */
type Integer = { toString(): string };
type Variant = Record<string, unknown>;

interface RawMilestone {
  name: string;
  bps: number;
  amount: Integer;
  gate: number;
  status: Variant;
  released_slot: Integer;
  approved_by: PublicKey;
  evidence_hash: number[];
}

interface RawPoEscrow {
  chain_po_id: string;
  buyer: PublicKey;
  payout: PublicKey;
  payout_token: PublicKey;
  mint: PublicKey;
  vault: PublicKey;
  po_hash: number[];
  cad_hash: number[];
  total: Integer;
  released: Integer;
  status: Variant;
  credential: PublicKey;
  schema: PublicKey;
  gates: { attester: PublicKey; attestation: PublicKey }[];
  milestones: RawMilestone[];
  refund_after: Integer;
  bump: number;
}

interface RawConfig {
  admin: PublicKey;
  operator: PublicKey;
  allowed_mints: [PublicKey, PublicKey];
  sas_credential: PublicKey;
  sas_schema: PublicKey;
  bump: number;
}

/** Decodes `PoEscrow` account data; throws if it is not one (discriminator, length, variant). */
export function decodePoEscrow(data: Uint8Array): PoEscrowAccount {
  const raw = escrowCoder.accounts.decode<RawPoEscrow>("PoEscrow", Buffer.from(data));
  return {
    chainPoId: raw.chain_po_id,
    buyer: raw.buyer.toBase58(),
    payout: raw.payout.toBase58(),
    payoutToken: raw.payout_token.toBase58(),
    mint: raw.mint.toBase58(),
    vault: raw.vault.toBase58(),
    poHash: toHex(raw.po_hash),
    cadHash: toHex(raw.cad_hash),
    total: BigInt(raw.total.toString()),
    released: BigInt(raw.released.toString()),
    status: variant("EscrowStatus", ESCROW_STATUSES, raw.status),
    credential: raw.credential.toBase58(),
    schema: raw.schema.toBase58(),
    gates: raw.gates.map((gate) => ({
      attester: gate.attester.toBase58(),
      attestation: gate.attestation.toBase58(),
    })),
    milestones: raw.milestones.map((milestone) => ({
      name: milestone.name,
      bps: milestone.bps,
      amount: BigInt(milestone.amount.toString()),
      gate: milestone.gate,
      status: variant("MilestoneStatus", MILESTONE_STATUSES, milestone.status),
      releasedSlot: BigInt(milestone.released_slot.toString()),
      approvedBy: milestone.approved_by.toBase58(),
      evidenceHash: toHex(milestone.evidence_hash),
    })),
    refundAfter: BigInt(raw.refund_after.toString()),
    bump: raw.bump,
  };
}

/** Decodes `Config` account data; throws if it is not one. */
export function decodeConfig(data: Uint8Array): ConfigAccount {
  const raw = escrowCoder.accounts.decode<RawConfig>("Config", Buffer.from(data));
  const [first, second] = raw.allowed_mints;
  return {
    admin: raw.admin.toBase58(),
    operator: raw.operator.toBase58(),
    allowedMints: [first.toBase58(), second.toBase58()],
    sasCredential: raw.sas_credential.toBase58(),
    sasSchema: raw.sas_schema.toBase58(),
    bump: raw.bump,
  };
}

// ---- builders ----

/** `create_po`: the escrow, vault, payout ATA and event authority are derived (classic SPL). */
export function createPoIx({
  programId,
  operator,
  payer,
  buyer,
  payout,
  mint,
  credential,
  args,
}: {
  programId: PublicKey;
  operator: PublicKey;
  payer: PublicKey;
  buyer: PublicKey;
  payout: PublicKey;
  mint: PublicKey;
  credential: PublicKey;
  args: CreatePoArgs;
}): TransactionInstruction {
  const [escrow] = escrowPda(programId, buyer, args.chainPoId);
  const accounts = {
    config: configPda(programId),
    operator,
    payer,
    buyer,
    payout,
    mint,
    escrow,
    vault: vaultAta(escrow, mint),
    payout_token: payoutAta(payout, mint),
    sas_credential: credential,
    token_program: TOKEN_PROGRAM_ID,
    event_authority: eventAuthorityPda(programId),
    program: programId,
  };
  const value = {
    chain_po_id: args.chainPoId,
    po_hash: Array.from(hashBytes("poHash", args.poHash)),
    cad_hash: Array.from(hashBytes("cadHash", args.cadHash)),
    total: new BN(unsigned("total", args.total, 64).toString()),
    gate_attesters: args.gateAttesters,
    milestones: args.milestones.map((milestone) => ({
      name: milestone.name,
      bps: Number(unsigned("bps", milestone.bps, 16)),
      gate: Number(unsigned("gate", milestone.gate, 8)),
    })),
    refund_after: new BN(signed64("refundAfter", args.refundAfter).toString()),
  };
  return programInstruction(programId, "create_po", accounts, { args: value });
}

/** `fund`, signed by `state.buyer`, from `buyerToken` into the escrow's vault. */
export function fundIx({
  programId,
  escrow,
  state,
  buyerToken,
}: {
  programId: PublicKey;
  escrow: PublicKey;
  state: PoEscrowAccount;
  buyerToken: PublicKey;
}): TransactionInstruction {
  const accounts = {
    buyer: new PublicKey(state.buyer),
    escrow,
    buyer_token: buyerToken,
    vault: new PublicKey(state.vault),
    mint: new PublicKey(state.mint),
    token_program: TOKEN_PROGRAM_ID,
    event_authority: eventAuthorityPda(programId),
    program: programId,
  };
  return programInstruction(programId, "fund", accounts, {});
}

/**
 * `release(milestone)` against its gate's stored attestation address. `attestation` overrides that
 * address; only the failed-release scene of spec 11 passes it.
 */
export function releaseIx({
  programId,
  escrow,
  state,
  milestone,
  attestation,
}: {
  programId: PublicKey;
  escrow: PublicKey;
  state: PoEscrowAccount;
  milestone: number;
  attestation?: PublicKey;
}): TransactionInstruction {
  if (!Number.isInteger(milestone) || milestone < 0 || milestone >= state.milestones.length) {
    throw new RangeError(
      `escrow has ${state.milestones.length} milestones, not milestone ${milestone}`,
    );
  }
  const gate = state.milestones[milestone].gate;
  const accounts = {
    escrow,
    vault: new PublicKey(state.vault),
    payout_token: new PublicKey(state.payoutToken),
    mint: new PublicKey(state.mint),
    attestation: attestation ?? new PublicKey(state.gates[gate].attestation),
    token_program: TOKEN_PROGRAM_ID,
    event_authority: eventAuthorityPda(programId),
    program: programId,
  };
  return programInstruction(programId, "release", accounts, { milestone });
}

/** `refund`, signed by `state.buyer`, back to the buyer's ATA (derived; it must exist). */
export function refundIx({
  programId,
  escrow,
  state,
}: {
  programId: PublicKey;
  escrow: PublicKey;
  state: PoEscrowAccount;
}): TransactionInstruction {
  const buyer = new PublicKey(state.buyer);
  const mint = new PublicKey(state.mint);
  const accounts = {
    buyer,
    escrow,
    vault: new PublicKey(state.vault),
    // The program's `associated_token::authority` accepts an off-curve buyer (a multisig vault).
    buyer_token: getAssociatedTokenAddressSync(mint, buyer, true),
    mint,
    token_program: TOKEN_PROGRAM_ID,
    event_authority: eventAuthorityPda(programId),
    program: programId,
  };
  return programInstruction(programId, "refund", accounts, {});
}

/** The SPL Memo `bido:payout_ref:<64 lowercase hex>` (80 bytes), with no accounts. */
export function payoutRefMemoIx(payoutRefHex: string): TransactionInstruction {
  if (!SHA256_HEX_RE.test(payoutRefHex)) {
    throw new RangeError(`payout_ref must be 64 lowercase hex digits, got "${payoutRefHex}"`);
  }
  const data = new TextEncoder().encode(`${PAYOUT_REF_MEMO_PREFIX}${payoutRefHex}`);
  return new TransactionInstruction({ programId: MEMO_PROGRAM, keys: [], data: Buffer.from(data) });
}

/** Instruction `name`: each IDL account in order, from `accounts` or its pinned address. */
function programInstruction(
  programId: PublicKey,
  name: string,
  accounts: Partial<Record<string, PublicKey>>,
  args: Record<string, unknown>,
): TransactionInstruction {
  const instruction = IDL.instructions.find((candidate) => candidate.name === name);
  if (instruction === undefined) {
    throw new Error(`${name} is not an instruction of the IDL`);
  }
  const names = new Set(instruction.accounts.map((account) => account.name));
  const unknown = Object.keys(accounts).filter((account) => !names.has(account));
  if (unknown.length > 0) {
    throw new Error(`${name}: accounts ${unknown.join(", ")} are not in the IDL`);
  }
  const keys = instruction.accounts.map((account): AccountMeta => {
    if ("accounts" in account) {
      throw new Error(`${name}: nested account group ${account.name} is not supported`);
    }
    const pinned = account.address === undefined ? undefined : new PublicKey(account.address);
    const pubkey = accounts[account.name] ?? pinned;
    if (pubkey === undefined) {
      throw new Error(`${name}: no address for account ${account.name}`);
    }
    if (pinned !== undefined && !pubkey.equals(pinned)) {
      throw new Error(`${name}: account ${account.name} must be ${pinned.toBase58()}`);
    }
    return { pubkey, isSigner: account.signer ?? false, isWritable: account.writable ?? false };
  });
  return new TransactionInstruction({
    programId,
    keys,
    data: escrowCoder.instruction.encode(name, args),
  });
}

function variant<T extends string>(type: string, names: readonly T[], value: Variant): T {
  const [name] = Object.keys(value);
  const known = names.find((candidate) => candidate === name);
  if (known === undefined) {
    throw new RangeError(`${type} has no variant ${name}`);
  }
  return known;
}

function unsigned(field: string, value: bigint | number, bits: number): bigint {
  if (typeof value === "number" && !Number.isInteger(value)) {
    throw new RangeError(`${field} must be an integer, got ${value}`);
  }
  const big = BigInt(value);
  if (BigInt.asUintN(bits, big) !== big) {
    throw new RangeError(`${field} ${value} does not fit in u${bits}`);
  }
  return big;
}

function signed64(field: string, value: bigint): bigint {
  if (BigInt.asIntN(64, value) !== value) {
    throw new RangeError(`${field} ${value} does not fit in i64`);
  }
  return value;
}

function hashBytes(field: string, hash: Uint8Array | string): Uint8Array {
  if (typeof hash !== "string") {
    if (hash.length !== HASH_LEN) {
      throw new RangeError(`${field} must be ${HASH_LEN} bytes, got ${hash.length}`);
    }
    return hash;
  }
  if (!SHA256_HEX_RE.test(hash)) {
    throw new RangeError(`${field} must be 64 lowercase hex digits, got "${hash}"`);
  }
  return Uint8Array.from({ length: HASH_LEN }, (_, i) =>
    parseInt(hash.slice(2 * i, 2 * i + 2), 16),
  );
}

function toHex(bytes: readonly number[]): string {
  return bytes.map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
