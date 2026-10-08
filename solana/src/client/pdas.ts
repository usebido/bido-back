// Addresses derived from seeds (spec 01 §3.4, §3.11): the escrow program's PDAs, the classic SPL
// Token associated token accounts it uses, and the SAS PDAs. Twins of
// `bido_back.chain.codec.pdas`; the `chain` section of `__fixtures__/vectors.json` pins them.

import { getAssociatedTokenAddressSync } from "@solana/spl-token";
import { PublicKey } from "@solana/web3.js";

import { SAS_PROGRAM_ID } from "../contracts";

const SAS_PROGRAM = new PublicKey(SAS_PROGRAM_ID);

const CONFIG_SEED = "config";
const PO_SEED = "po";
/** Anchor's `#[event_cpi]` event authority. */
const EVENT_AUTHORITY_SEED = "__event_authority";
const SAS_CREDENTIAL_SEED = "credential";
const SAS_SCHEMA_SEED = "schema";
const SAS_ATTESTATION_SEED = "attestation";
/** Solana's limit on one seed. */
const MAX_SEED_LEN = 32;

/** `Config` PDA `["config"]`. */
export function configPda(programId: PublicKey): PublicKey {
  return findPda([utf8(CONFIG_SEED)], programId)[0];
}

/** `PoEscrow` PDA `["po", buyer, chain_po_id]` and its bump. */
export function escrowPda(
  programId: PublicKey,
  buyer: PublicKey,
  chainPoId: string,
): [PublicKey, number] {
  return findPda([utf8(PO_SEED), buyer.toBytes(), utf8(chainPoId)], programId);
}

/** The escrow's vault: its associated token account for `mint`. */
export function vaultAta(escrow: PublicKey, mint: PublicKey): PublicKey {
  // The owner is a PDA, so it is off the curve.
  return getAssociatedTokenAddressSync(mint, escrow, true);
}

/** `payout_token`: the payout owner's associated token account for `mint`. */
export function payoutAta(payoutOwner: PublicKey, mint: PublicKey): PublicKey {
  // The program's `associated_token::authority` accepts an off-curve owner (a multisig vault).
  return getAssociatedTokenAddressSync(mint, payoutOwner, true);
}

/** The program's event authority PDA `["__event_authority"]` (`emit_cpi!`). */
export function eventAuthorityPda(programId: PublicKey): PublicKey {
  return findPda([utf8(EVENT_AUTHORITY_SEED)], programId)[0];
}

/** SAS credential PDA `["credential", issuer, name]`. */
export function credentialPda(issuer: PublicKey, name: string): PublicKey {
  return findPda([utf8(SAS_CREDENTIAL_SEED), issuer.toBytes(), utf8(name)], SAS_PROGRAM)[0];
}

/** SAS schema PDA `["schema", credential, name, u8 version]`. */
export function schemaPda(credential: PublicKey, name: string, version: number): PublicKey {
  if (!Number.isInteger(version) || version < 0 || version > 255) {
    throw new RangeError(`version must be a u8, got ${version}`);
  }
  const seeds = [utf8(SAS_SCHEMA_SEED), credential.toBytes(), utf8(name), Uint8Array.of(version)];
  return findPda(seeds, SAS_PROGRAM)[0];
}

/**
 * SAS attestation PDA `["attestation", credential, schema, nonce]`. `nonce` is 32 bytes, such as
 * `gateNonce(escrow, gate)`, or the same as a `PublicKey`.
 */
export function attestationPda(
  credential: PublicKey,
  schema: PublicKey,
  nonce: PublicKey | Uint8Array,
): PublicKey {
  const nonceBytes = nonce instanceof PublicKey ? nonce.toBytes() : nonce;
  if (nonceBytes.length !== 32) {
    throw new RangeError(`nonce must be 32 bytes, got ${nonceBytes.length}`);
  }
  const seeds = [utf8(SAS_ATTESTATION_SEED), credential.toBytes(), schema.toBytes(), nonceBytes];
  return findPda(seeds, SAS_PROGRAM)[0];
}

function findPda(seeds: Uint8Array[], programId: PublicKey): [PublicKey, number] {
  for (const seed of seeds) {
    if (seed.length > MAX_SEED_LEN) {
      throw new RangeError(`PDA seed of ${seed.length} bytes is longer than ${MAX_SEED_LEN} bytes`);
    }
  }
  return PublicKey.findProgramAddressSync(seeds, programId);
}

function utf8(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}
