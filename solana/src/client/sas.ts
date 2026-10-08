// The SAS side of an approval (spec 01 §3.6, §3.9, §3.11): the 73-byte attestation data of schema
// `bido-gate-approval` v1, the hand-encoded `CreateAttestation` instruction (golden-tested against
// `@solana/attestation` 2.1.0) and the decoders the browser's sign guards and the audit trail use
// (spec 02 §3.10, spec 05). Twin of `bido_back.chain.codec.sas`. Hashes are lowercase hex and
// addresses base58 in every decoded value, as in spec 02's `DecodedIx`.

import { PublicKey, SystemProgram, TransactionInstruction } from "@solana/web3.js";

import { SAS_PROGRAM_ID } from "../contracts";

import { attestationPda } from "./pdas";

const SAS_PROGRAM = new PublicKey(SAS_PROGRAM_ID);

/** SAS instruction tag of `CreateAttestation`. */
const CREATE_ATTESTATION_TAG = 6;
/** SAS account discriminator of an `Attestation`. */
const ATTESTATION_DISC = 2;
const HASH_LEN = 32;
const ADDRESS_LEN = 32;
/** `u32 LE 32 ‖ po_hash ‖ u8 gate ‖ u32 LE 32 ‖ evidence_hash`. */
const GATE_DATA_LEN = 4 + HASH_LEN + 1 + 4 + HASH_LEN; // 73
/** `tag ‖ nonce ‖ u32 LE data length ‖ data ‖ i64 LE expiry` with our 73-byte data. */
const CREATE_ATTESTATION_DATA_LEN = 1 + ADDRESS_LEN + 4 + GATE_DATA_LEN + 8; // 118
/** disc, nonce, credential, schema, u32 data length, data, signer, i64 expiry, token_account. */
const ATTESTATION_ACCOUNT_LEN =
  1 + 3 * ADDRESS_LEN + 4 + GATE_DATA_LEN + ADDRESS_LEN + 8 + ADDRESS_LEN; // 246

/** Offsets in an attestation account (§3.6). */
const ATT_NONCE = 1;
const ATT_CREDENTIAL = 33;
const ATT_SCHEMA = 65;
const ATT_DATA_LEN = 97;
const ATT_DATA = 101;
const ATT_SIGNER = ATT_DATA + GATE_DATA_LEN; // 174
const ATT_EXPIRY = 206;
const ATT_TOKEN_ACCOUNT = 214;

const SHA256_HEX_RE = /^[0-9a-f]{64}$/;

/** The fields of the schema `bido-gate-approval` v1 (`SAS_SCHEMA_FIELDS`). */
export interface GateApprovalData {
  /** sha256 of the PO PDF, lowercase hex. */
  poHash: string;
  gate: number;
  /** `computeEvidenceHash(…)`, lowercase hex. */
  evidenceHash: string;
}

/** An attestation account of our schema, decoded at the §3.6 offsets. */
export interface GateAttestation extends GateApprovalData {
  nonce: string;
  credential: string;
  schema: string;
  /** The authorised signer who created it (checked against `gates[g].attester` at release). */
  signer: string;
  /** Unix seconds; 0 = never expires (Bido always writes 0). */
  expiry: bigint;
}

/**
 * The 73-byte attestation data `u32 LE 32 ‖ po_hash ‖ u8 gate ‖ u32 LE 32 ‖ evidence_hash`. Each
 * hash is 32 bytes or 64 lowercase hex digits.
 */
export function encodeGateApprovalData({
  poHash,
  gate,
  evidenceHash,
}: {
  poHash: Uint8Array | string;
  gate: number;
  evidenceHash: Uint8Array | string;
}): Uint8Array {
  if (!Number.isInteger(gate) || gate < 0 || gate > 255) {
    throw new RangeError(`gate must be a u8, got ${gate}`);
  }
  const out = new Uint8Array(GATE_DATA_LEN);
  const view = new DataView(out.buffer);
  view.setUint32(0, HASH_LEN, true);
  out.set(hashBytes("poHash", poHash), 4);
  out[4 + HASH_LEN] = gate;
  view.setUint32(4 + HASH_LEN + 1, HASH_LEN, true);
  out.set(hashBytes("evidenceHash", evidenceHash), 4 + HASH_LEN + 1 + 4);
  return out;
}

/** Inverse of `encodeGateApprovalData`; throws a `RangeError` unless `data` has that exact shape. */
export function decodeGateApprovalData(data: Uint8Array): GateApprovalData {
  if (data.length !== GATE_DATA_LEN) {
    throw new RangeError(`Gate approval data must be ${GATE_DATA_LEN} bytes, got ${data.length}`);
  }
  const view = viewOf(data);
  if (view.getUint32(0, true) !== HASH_LEN || view.getUint32(4 + HASH_LEN + 1, true) !== HASH_LEN) {
    throw new RangeError(`Gate approval data hashes must be ${HASH_LEN}-byte vectors`);
  }
  return {
    poHash: toHex(data.subarray(4, 4 + HASH_LEN)),
    gate: data[4 + HASH_LEN],
    evidenceHash: toHex(data.subarray(4 + HASH_LEN + 1 + 4)),
  };
}

/**
 * SAS `CreateAttestation` (tag 6): accounts `[payer (w, s), signer (s), credential, schema,
 * attestation (w), system_program]`, the attestation being `attestationPda(credential, schema,
 * nonce)`; data `0x06 ‖ nonce ‖ u32 LE data length ‖ data ‖ i64 LE expiry`. In an approval `payer`
 * is the relayer, `signer` the gate's attester and `data` is `encodeGateApprovalData(…)` (§3.9).
 */
export function createAttestationIx({
  payer,
  signer,
  credential,
  schema,
  nonce,
  data,
  expiry = BigInt(0),
}: {
  payer: PublicKey;
  signer: PublicKey;
  credential: PublicKey;
  schema: PublicKey;
  nonce: PublicKey | Uint8Array;
  data: Uint8Array;
  expiry?: bigint;
}): TransactionInstruction {
  if (BigInt.asIntN(64, expiry) !== expiry) {
    throw new RangeError(`expiry must be an i64, got ${expiry}`);
  }
  const nonceBytes = nonce instanceof PublicKey ? nonce.toBytes() : nonce;
  // Also checks that the nonce is 32 bytes.
  const attestation = attestationPda(credential, schema, nonceBytes);
  const ixData = new Uint8Array(1 + ADDRESS_LEN + 4 + data.length + 8);
  const view = new DataView(ixData.buffer);
  ixData[0] = CREATE_ATTESTATION_TAG;
  ixData.set(nonceBytes, 1);
  view.setUint32(1 + ADDRESS_LEN, data.length, true);
  ixData.set(data, 1 + ADDRESS_LEN + 4);
  view.setBigInt64(1 + ADDRESS_LEN + 4 + data.length, expiry, true);
  return new TransactionInstruction({
    programId: SAS_PROGRAM,
    keys: [
      { pubkey: payer, isSigner: true, isWritable: true },
      { pubkey: signer, isSigner: true, isWritable: false },
      { pubkey: credential, isSigner: false, isWritable: false },
      { pubkey: schema, isSigner: false, isWritable: false },
      { pubkey: attestation, isSigner: false, isWritable: true },
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
    ],
    data: Buffer.from(ixData),
  });
}

/**
 * Decodes the data of a Bido `CreateAttestation` instruction (its 118-byte layout, §3.9) for the
 * sign guards; throws a `RangeError` on any other tag, length or data shape.
 */
export function decodeCreateAttestationData(ixData: Uint8Array): {
  nonce: string;
  data: GateApprovalData;
  expiry: bigint;
} {
  if (ixData.length !== CREATE_ATTESTATION_DATA_LEN) {
    throw new RangeError(
      `CreateAttestation data must be ${CREATE_ATTESTATION_DATA_LEN} bytes, got ${ixData.length}`,
    );
  }
  if (ixData[0] !== CREATE_ATTESTATION_TAG) {
    throw new RangeError(`Not a CreateAttestation instruction (tag ${ixData[0]})`);
  }
  const view = viewOf(ixData);
  const dataStart = 1 + ADDRESS_LEN + 4;
  if (view.getUint32(1 + ADDRESS_LEN, true) !== GATE_DATA_LEN) {
    throw new RangeError(`CreateAttestation data length must be ${GATE_DATA_LEN}`);
  }
  return {
    nonce: toBase58(ixData.subarray(1, 1 + ADDRESS_LEN)),
    data: decodeGateApprovalData(ixData.subarray(dataStart, dataStart + GATE_DATA_LEN)),
    expiry: view.getBigInt64(dataStart + GATE_DATA_LEN, true),
  };
}

/**
 * Decodes an attestation account of our schema at the §3.6 offsets. Throws a `RangeError` on any
 * shape violation the program rejects as `AttestationMalformed`: length ≠ 246, discriminator ≠ 2,
 * data length ≠ 73, an inner hash length ≠ 32, or a token account (tokenized attestation).
 */
export function decodeGateAttestation(accountData: Uint8Array): GateAttestation {
  if (accountData.length !== ATTESTATION_ACCOUNT_LEN) {
    throw new RangeError(
      `Attestation account must be ${ATTESTATION_ACCOUNT_LEN} bytes, got ${accountData.length}`,
    );
  }
  if (accountData[0] !== ATTESTATION_DISC) {
    throw new RangeError(`Not an attestation account (discriminator ${accountData[0]})`);
  }
  const view = viewOf(accountData);
  if (view.getUint32(ATT_DATA_LEN, true) !== GATE_DATA_LEN) {
    throw new RangeError(`Attestation data length must be ${GATE_DATA_LEN}`);
  }
  if (accountData.subarray(ATT_TOKEN_ACCOUNT).some((byte) => byte !== 0)) {
    throw new RangeError("Attestation is tokenized (token_account is set)");
  }
  return {
    nonce: toBase58(accountData.subarray(ATT_NONCE, ATT_CREDENTIAL)),
    credential: toBase58(accountData.subarray(ATT_CREDENTIAL, ATT_SCHEMA)),
    schema: toBase58(accountData.subarray(ATT_SCHEMA, ATT_DATA_LEN)),
    ...decodeGateApprovalData(accountData.subarray(ATT_DATA, ATT_SIGNER)),
    signer: toBase58(accountData.subarray(ATT_SIGNER, ATT_EXPIRY)),
    expiry: view.getBigInt64(ATT_EXPIRY, true),
  };
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

function viewOf(bytes: Uint8Array): DataView {
  return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
}

function toHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function toBase58(bytes: Uint8Array): string {
  return new PublicKey(bytes).toBase58();
}
