// The hashes that bind an approval to one escrow and one set of evidence files (spec 00 §3.3).
// Twins of `bido_back.chain.codec.hashes`; the `chain` section of `__fixtures__/vectors.json`
// (spec 01 §3.10) pins both. Synchronous, because `checkSignGuards` (spec 02 §3.10) is.

import { GATE_NONCE_DOMAIN } from "../contracts";

/** Domain prefix of `evidence_hash`: `sha256("bido-evidence-v1" ‖ chain_po_id ‖ u8 gate ‖ …)`. */
const EVIDENCE_HASH_DOMAIN = "bido-evidence-v1";
/** A sha256 as stored and sent everywhere (`sha256_hex`, spec 03): 64 lowercase hex digits. */
const SHA256_HEX_RE = /^[0-9a-f]{64}$/;
const ADDRESS_LEN = 32;

/**
 * Prefix of the `create_po` intake's SPL Memo `bido:payout_ref:<64 lowercase hex>` (spec 08;
 * `bido_back.sim.payout_ref` is the source). Kept here while `payoutRefMemoIx` uses it.
 */
export const PAYOUT_REF_MEMO_PREFIX = "bido:payout_ref:";

/**
 * The SAS attestation nonce of gate `gate`: `sha256("bido-gate-v1" ‖ escrow ‖ u8 gate)`, 32 bytes.
 * `escrow` is the escrow address as bytes or as anything with `toBytes()` (a web3.js `PublicKey`).
 */
export function gateNonce(
  escrow: Uint8Array | { toBytes(): Uint8Array },
  gate: number,
): Uint8Array {
  const address = "toBytes" in escrow ? escrow.toBytes() : escrow;
  if (address.length !== ADDRESS_LEN) {
    throw new RangeError(`escrow must be ${ADDRESS_LEN} bytes, got ${address.length}`);
  }
  return sha256(concatBytes([utf8(GATE_NONCE_DOMAIN), address, gateByte(gate)]));
}

/**
 * `evidence_hash = sha256("bido-evidence-v1" ‖ chain_po_id ‖ u8 gate ‖ sorted file hashes)` as
 * lowercase hex. `fileHashes` are the sha256 of each evidence file (lowercase hex), in any order.
 */
export function computeEvidenceHash(
  chainPoId: string,
  gate: number,
  fileHashes: readonly string[],
): string {
  for (const fileHash of fileHashes) {
    if (!SHA256_HEX_RE.test(fileHash)) {
      throw new RangeError(`File hash must be 64 lowercase hex digits, got "${fileHash}"`);
    }
  }
  // Equal-length lowercase hex strings sort in the order of their bytes.
  const sorted = [...fileHashes].sort().map(hexToBytes);
  const preimage = concatBytes([
    utf8(EVIDENCE_HASH_DOMAIN),
    utf8(chainPoId),
    gateByte(gate),
    ...sorted,
  ]);
  return bytesToHex(sha256(preimage));
}

// ---- SHA-256 (FIPS 180-4) ----
// `node:crypto` is not client-safe and WebCrypto's digest is async, so the browser gets this small
// synchronous one; `hashes.test.ts` checks it against `node:crypto` and the FIPS examples.

/** First 32 bits of the fractional parts of the cube roots of the first 64 primes. */
const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);
/** First 32 bits of the fractional parts of the square roots of the first 8 primes. */
const H0 = [
  0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
];

function rotr(x: number, n: number): number {
  return (x >>> n) | (x << (32 - n));
}

/** The 32-byte SHA-256 digest of `data`. */
export function sha256(data: Uint8Array): Uint8Array {
  // Padding: 0x80, zeros up to 56 mod 64, then the message length in bits as a big-endian u64.
  const padded = new Uint8Array(Math.ceil((data.length + 9) / 64) * 64);
  padded.set(data);
  padded[data.length] = 0x80;
  const view = new DataView(padded.buffer);
  const bits = data.length * 8;
  view.setUint32(padded.length - 8, Math.floor(bits / 0x1_0000_0000));
  view.setUint32(padded.length - 4, bits >>> 0);

  const h = H0.slice();
  const w = new Uint32Array(64); // assignments wrap mod 2^32
  for (let offset = 0; offset < padded.length; offset += 64) {
    for (let t = 0; t < 16; t++) w[t] = view.getUint32(offset + 4 * t);
    for (let t = 16; t < 64; t++) {
      const s0 = rotr(w[t - 15], 7) ^ rotr(w[t - 15], 18) ^ (w[t - 15] >>> 3);
      const s1 = rotr(w[t - 2], 17) ^ rotr(w[t - 2], 19) ^ (w[t - 2] >>> 10);
      w[t] = w[t - 16] + s0 + w[t - 7] + s1;
    }
    let [a, b, c, d, e, f, g, hh] = h;
    for (let t = 0; t < 64; t++) {
      const t1 =
        (hh + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + K[t] + w[t]) | 0;
      const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) | 0;
      hh = g;
      g = f;
      f = e;
      e = (d + t1) | 0;
      d = c;
      c = b;
      b = a;
      a = (t1 + t2) | 0;
    }
    [a, b, c, d, e, f, g, hh].forEach((word, i) => {
      h[i] = (h[i] + word) | 0;
    });
  }

  const digest = new Uint8Array(32);
  const out = new DataView(digest.buffer);
  h.forEach((word, i) => out.setUint32(4 * i, word >>> 0));
  return digest;
}

// ---- bytes ----

function gateByte(gate: number): Uint8Array {
  if (!Number.isInteger(gate) || gate < 0 || gate > 255) {
    throw new RangeError(`gate must be a u8, got ${gate}`);
  }
  return Uint8Array.of(gate);
}

function utf8(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

function concatBytes(parts: readonly Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((len, part) => len + part.length, 0));
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}

function hexToBytes(hex: string): Uint8Array {
  return Uint8Array.from({ length: hex.length / 2 }, (_, i) =>
    parseInt(hex.slice(2 * i, 2 * i + 2), 16),
  );
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
