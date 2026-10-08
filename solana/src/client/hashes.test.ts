// @vitest-environment node
import { createHash, randomBytes } from "node:crypto";
import { describe, expect, it } from "vitest";

import vectors from "../../tests/fixtures/vectors.json";
import { computeEvidenceHash, gateNonce, sha256 } from "./hashes";

// The `chain` section of the golden vectors (spec 01 §3.10, written by `vectors.rs`).
const chain = vectors.chain;

const BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

/** Base58 → bytes; the vectors write addresses in base58. */
function base58ToBytes(text: string): Uint8Array {
  let value = BigInt(0);
  for (const char of text) {
    const digit = BASE58_ALPHABET.indexOf(char);
    if (digit < 0) throw new Error(`Not base58: ${text}`);
    value = value * BigInt(58) + BigInt(digit);
  }
  const bytes: number[] = [];
  for (; value > BigInt(0); value /= BigInt(256)) bytes.unshift(Number(value % BigInt(256)));
  for (const char of text) {
    if (char !== "1") break;
    bytes.unshift(0);
  }
  return Uint8Array.from(bytes);
}

const toHex = (bytes: Uint8Array) => Buffer.from(bytes).toString("hex");
const utf8 = (text: string) => new TextEncoder().encode(text);

describe("gateNonce", () => {
  const escrow = base58ToBytes(chain.escrow);

  it("equals the vectors' nonce of each gate", () => {
    expect(escrow).toHaveLength(32);
    expect(chain.gates.map((g) => g.gate)).toEqual([0, 1]);
    for (const g of chain.gates) {
      expect(gateNonce(escrow, g.gate)).toEqual(base58ToBytes(g.nonce));
    }
  });

  it("takes an address with toBytes(), such as a web3.js PublicKey", () => {
    expect(gateNonce({ toBytes: () => escrow }, 1)).toEqual(base58ToBytes(chain.gates[1].nonce));
  });

  it("rejects a gate that is not a u8 and an address that is not 32 bytes", () => {
    for (const gate of [-1, 256, 1.5, Number.NaN]) {
      expect(() => gateNonce(escrow, gate)).toThrow(RangeError);
    }
    expect(() => gateNonce(escrow.subarray(1), 0)).toThrow(RangeError);
  });
});

describe("computeEvidenceHash", () => {
  it("equals the vectors' evidence_hash of each case", () => {
    expect(chain.evidence_hashes).toHaveLength(3);
    for (const c of chain.evidence_hashes) {
      expect(computeEvidenceHash(c.chain_po_id, c.gate, c.file_hashes)).toBe(c.evidence_hash);
    }
    for (const g of chain.gates) {
      const [first] = chain.evidence_hashes.filter((c) => c.gate === g.gate);
      expect(computeEvidenceHash(chain.inputs.chain_po_id, g.gate, first.file_hashes)).toBe(
        g.evidence_hash,
      );
    }
  });

  it("sorts the file hashes and binds the PO id and the gate", () => {
    const [gate0] = chain.evidence_hashes;
    const files = gate0.file_hashes;
    const id = gate0.chain_po_id;
    const hash = computeEvidenceHash(id, 0, files);
    expect(computeEvidenceHash(id, 0, [...files].reverse())).toBe(hash);
    expect(computeEvidenceHash(id, 0, [files[1], files[2], files[0]])).toBe(hash);
    expect(computeEvidenceHash(id, 1, files)).not.toBe(hash);
    expect(computeEvidenceHash("PO-1042-7KQ3", 0, files)).not.toBe(hash);
    expect(computeEvidenceHash(id, 0, files.slice(1))).not.toBe(hash);
  });

  it("hashes the fixed evidence files of the vectors", () => {
    const files = [1, 2, 3].map((n) => toHex(sha256(utf8(`bido-vectors:evidence-file-${n}`))));
    expect([...files].sort()).toEqual([...chain.evidence_hashes[0].file_hashes].sort());
  });

  it("rejects file hashes that are not 64 lowercase hex digits and a gate that is not a u8", () => {
    const [valid] = chain.evidence_hashes[0].file_hashes;
    for (const bad of [valid.toUpperCase(), valid.slice(1), `0x${valid.slice(2)}`, `${valid}00`]) {
      expect(() => computeEvidenceHash("PO-1042-7KQ2", 0, [valid, bad])).toThrow(RangeError);
    }
    expect(() => computeEvidenceHash("PO-1042-7KQ2", 256, [valid])).toThrow(RangeError);
  });
});

describe("sha256", () => {
  it("gives the FIPS 180-4 example digests", () => {
    expect(toHex(sha256(utf8("")))).toBe(
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    );
    expect(toHex(sha256(utf8("abc")))).toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
    expect(toHex(sha256(utf8("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq")))).toBe(
      "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
    );
  });

  it("matches node:crypto across the padding boundaries and over many blocks", () => {
    const lengths = [...Array.from({ length: 200 }, (_, i) => i), 1_000, 65_536 + 7];
    for (const length of lengths) {
      const data = randomBytes(length);
      expect(toHex(sha256(data)), `length ${length}`).toBe(
        createHash("sha256").update(data).digest("hex"),
      );
    }
  });
});
