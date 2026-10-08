// The on-chain PO id `chain_po_id` (spec 00 §3.3). `newChainPoId` is the twin of
// `bido_back.chain.codec.hashes.new_chain_po_id`, which the backend uses at intake.

import { CHAIN_PO_ID_RE, MAX_CHAIN_PO_ID_LEN } from "../contracts";

/** Crockford base32: digits and capitals without I, L, O and U. */
const CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const SUFFIX_LEN = 4;

/**
 * `<poNumber>-<4 random Crockford base32 characters>`, e.g. `PO-1042-7KQ2`. Throws a `RangeError`
 * unless the result matches `CHAIN_PO_ID_RE`, which also caps it at 32 bytes: `poNumber` must be 1
 * to 27 of `[A-Z0-9-]` (the intake's V1 normalises it first, spec 04).
 */
export function newChainPoId(poNumber: string): string {
  // 256 is a multiple of 32, so each random byte picks a character uniformly.
  const suffix = Array.from(
    crypto.getRandomValues(new Uint8Array(SUFFIX_LEN)),
    (byte) => CROCKFORD_BASE32[byte % CROCKFORD_BASE32.length],
  ).join("");
  const chainPoId = `${poNumber}-${suffix}`;
  if (!CHAIN_PO_ID_RE.test(chainPoId)) {
    throw new RangeError(`PO number "${poNumber}" does not make a valid chain_po_id`);
  }
  return chainPoId;
}

/**
 * Whether `id` is a well-formed `chain_po_id`: it matches `CHAIN_PO_ID_RE` and fits the 32-byte
 * PDA seed. The `/po/[id]` route checks it before any request.
 */
export function isChainPoId(id: string): boolean {
  return CHAIN_PO_ID_RE.test(id) && new TextEncoder().encode(id).length <= MAX_CHAIN_PO_ID_LEN;
}
