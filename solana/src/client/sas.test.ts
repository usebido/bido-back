// @vitest-environment node
import { createHash } from "node:crypto";

import {
  deserializeAttestationData,
  getAttestationEncoder,
  getCreateAttestationInstruction,
  type Schema,
  serializeAttestationData,
} from "@solana/attestation";
import { AccountRole, address, createNoopSigner } from "@solana/kit";
import { Keypair, PublicKey, SystemProgram } from "@solana/web3.js";
import { describe, expect, it } from "vitest";

import {
  SAS_PROGRAM_ID,
  SAS_SCHEMA_FIELDS,
  SAS_SCHEMA_LAYOUT,
  SAS_SCHEMA_NAME,
  SAS_SCHEMA_VERSION,
} from "../contracts";

import vectors from "../../tests/fixtures/vectors.json";
import {
  createAttestationIx,
  decodeCreateAttestationData,
  decodeGateApprovalData,
  decodeGateAttestation,
  encodeGateApprovalData,
} from "./sas";

// The `chain` section of the golden vectors (spec 01 §3.10, written by `vectors.rs`).
const chain = vectors.chain;
const inputs = chain.inputs;

const pk = (base58: string) => new PublicKey(base58);
const toHex = (bytes: Uint8Array) => Buffer.from(bytes).toString("hex");
const fromHex = (hex: string) => new Uint8Array(Buffer.from(hex, "hex"));

/** The harness's throwaway `test_keypair(label)`: seed `sha256("bido-escrow-test:" ‖ label)`. */
function harnessPubkey(label: string): PublicKey {
  const seed = createHash("sha256").update(`bido-escrow-test:${label}`).digest();
  return Keypair.fromSeed(seed).publicKey;
}

/** The on-chain schema `bido-gate-approval` v1 as `@solana/attestation` reads it. */
const SCHEMA: Schema = {
  discriminator: 1,
  credential: address(inputs.credential),
  name: SAS_SCHEMA_NAME,
  description: "",
  layout: [...SAS_SCHEMA_LAYOUT],
  fieldNames: [...SAS_SCHEMA_FIELDS],
  isPaused: false,
  version: SAS_SCHEMA_VERSION,
};

const relayer = harnessPubkey("relayer");
/** Gate attesters of the harness PO: gate 0 the buyer, gate 1 the SQE. */
const attesters = [harnessPubkey("buyer"), harnessPubkey("sqe")];
const credential = pk(inputs.credential);
const schema = pk(inputs.schema);

describe("encodeGateApprovalData", () => {
  it("equals the vectors' attestation data of each gate", () => {
    expect(chain.gates.map((g) => g.gate)).toEqual([0, 1]);
    for (const g of chain.gates) {
      const data = encodeGateApprovalData({
        poHash: inputs.po_hash,
        gate: g.gate,
        evidenceHash: g.evidence_hash,
      });
      expect(data).toHaveLength(73);
      expect(toHex(data)).toBe(g.attestation_data);
    }
  });

  it("equals @solana/attestation's serialization for the schema's layout", () => {
    for (const g of chain.gates) {
      const expected = serializeAttestationData(SCHEMA, {
        po_hash: Array.from(fromHex(inputs.po_hash)),
        gate: g.gate,
        evidence_hash: Array.from(fromHex(g.evidence_hash)),
      });
      const data = encodeGateApprovalData({
        poHash: fromHex(inputs.po_hash),
        gate: g.gate,
        evidenceHash: fromHex(g.evidence_hash),
      });
      expect(data).toEqual(expected);
    }
  });

  it("rejects a hash that is not 32 bytes or 64 lowercase hex digits and a gate that is not a u8", () => {
    const evidenceHash = chain.gates[0].evidence_hash;
    for (const bad of [
      inputs.po_hash.toUpperCase().replace(/5/g, "A"),
      inputs.po_hash.slice(2),
      `${inputs.po_hash}00`,
      fromHex(inputs.po_hash).subarray(1),
    ]) {
      expect(() => encodeGateApprovalData({ poHash: bad, gate: 0, evidenceHash })).toThrow(
        RangeError,
      );
    }
    for (const gate of [-1, 256, 0.5, Number.NaN]) {
      expect(() => encodeGateApprovalData({ poHash: inputs.po_hash, gate, evidenceHash })).toThrow(
        RangeError,
      );
    }
  });
});

describe("decodeGateApprovalData", () => {
  it("inverts encodeGateApprovalData and agrees with @solana/attestation", () => {
    for (const g of chain.gates) {
      const data = fromHex(g.attestation_data);
      expect(decodeGateApprovalData(data)).toEqual({
        poHash: inputs.po_hash,
        gate: g.gate,
        evidenceHash: g.evidence_hash,
      });
      const parsed = deserializeAttestationData<{
        po_hash: number[];
        gate: number;
        evidence_hash: number[];
      }>(SCHEMA, data);
      expect(toHex(Uint8Array.from(parsed.evidence_hash))).toBe(g.evidence_hash);
      expect(parsed.gate).toBe(g.gate);
    }
  });

  it("rejects another length or an inner hash length other than 32", () => {
    const data = fromHex(chain.gates[1].attestation_data);
    expect(() => decodeGateApprovalData(data.subarray(1))).toThrow(RangeError);
    expect(() => decodeGateApprovalData(Uint8Array.of(...data, 0))).toThrow(RangeError);
    for (const offset of [0, 37]) {
      const forged = data.slice();
      forged[offset] = 31;
      expect(() => decodeGateApprovalData(forged)).toThrow(RangeError);
    }
  });
});

describe("createAttestationIx", () => {
  it("equals the vectors' CreateAttestation data and the §3.9 accounts for each gate", () => {
    for (const g of chain.gates) {
      const ix = createAttestationIx({
        payer: relayer,
        signer: attesters[g.gate],
        credential,
        schema,
        nonce: pk(g.nonce),
        data: fromHex(g.attestation_data),
      });
      expect(ix.data).toHaveLength(118);
      expect(toHex(ix.data)).toBe(g.create_attestation_data);
      expect(ix.programId.toBase58()).toBe(SAS_PROGRAM_ID);
      expect(ix.keys).toEqual([
        { pubkey: relayer, isSigner: true, isWritable: true },
        { pubkey: attesters[g.gate], isSigner: true, isWritable: false },
        { pubkey: credential, isSigner: false, isWritable: false },
        { pubkey: schema, isSigner: false, isWritable: false },
        { pubkey: pk(g.attestation), isSigner: false, isWritable: true },
        { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
      ]);
    }
  });

  it("equals @solana/attestation's getCreateAttestationInstruction, bytes and accounts", () => {
    const g = chain.gates[1];
    const data = fromHex(g.attestation_data);
    for (const expiry of [0, 1_760_000_000, -1]) {
      const ix = createAttestationIx({
        payer: relayer,
        signer: attesters[1],
        credential,
        schema,
        nonce: pk(g.nonce).toBytes(),
        data,
        expiry: BigInt(expiry),
      });
      const kitIx = getCreateAttestationInstruction({
        payer: createNoopSigner(address(relayer.toBase58())),
        authority: createNoopSigner(address(attesters[1].toBase58())),
        credential: address(inputs.credential),
        schema: address(inputs.schema),
        attestation: address(g.attestation),
        nonce: address(g.nonce),
        data,
        expiry,
      });
      expect(new Uint8Array(ix.data)).toEqual(new Uint8Array(kitIx.data));
      expect(kitIx.programAddress).toBe(ix.programId.toBase58());
      expect(kitIx.accounts.map((meta) => ({ address: meta.address, role: meta.role }))).toEqual(
        ix.keys.map((key) => ({
          address: key.pubkey.toBase58(),
          role: key.isSigner
            ? key.isWritable
              ? AccountRole.WRITABLE_SIGNER
              : AccountRole.READONLY_SIGNER
            : key.isWritable
              ? AccountRole.WRITABLE
              : AccountRole.READONLY,
        })),
      );
    }
  });

  it("rejects an expiry outside i64 and a nonce that is not 32 bytes", () => {
    const base = {
      payer: relayer,
      signer: attesters[0],
      credential,
      schema,
      nonce: pk(chain.gates[0].nonce),
      data: fromHex(chain.gates[0].attestation_data),
    };
    expect(() => createAttestationIx({ ...base, expiry: BigInt(2) ** BigInt(63) })).toThrow(
      RangeError,
    );
    createAttestationIx({ ...base, expiry: BigInt(2) ** BigInt(63) - BigInt(1) });
    expect(() =>
      createAttestationIx({ ...base, nonce: pk(chain.gates[0].nonce).toBytes().subarray(1) }),
    ).toThrow(RangeError);
  });
});

describe("decodeCreateAttestationData", () => {
  it("decodes the vectors' CreateAttestation data of each gate", () => {
    for (const g of chain.gates) {
      expect(decodeCreateAttestationData(fromHex(g.create_attestation_data))).toEqual({
        nonce: g.nonce,
        data: { poHash: inputs.po_hash, gate: g.gate, evidenceHash: g.evidence_hash },
        expiry: BigInt(0),
      });
    }
  });

  it("returns a non-zero expiry as a signed i64", () => {
    const g = chain.gates[0];
    const ix = createAttestationIx({
      payer: relayer,
      signer: attesters[0],
      credential,
      schema,
      nonce: pk(g.nonce),
      data: fromHex(g.attestation_data),
      expiry: BigInt(-60),
    });
    expect(decodeCreateAttestationData(ix.data).expiry).toBe(BigInt(-60));
  });

  it("rejects another length, another tag and a data length other than 73", () => {
    const ixData = fromHex(chain.gates[0].create_attestation_data);
    expect(() => decodeCreateAttestationData(ixData.subarray(1))).toThrow(RangeError);
    expect(() => decodeCreateAttestationData(Uint8Array.of(...ixData, 0))).toThrow(RangeError);
    const otherTag = ixData.slice();
    otherTag[0] = 7;
    expect(() => decodeCreateAttestationData(otherTag)).toThrow(RangeError);
    const otherLength = ixData.slice();
    otherLength[33] = 72;
    expect(() => decodeCreateAttestationData(otherLength)).toThrow(RangeError);
    const otherInner = ixData.slice();
    otherInner[37] = 33;
    expect(() => decodeCreateAttestationData(otherInner)).toThrow(RangeError);
  });
});

describe("decodeGateAttestation", () => {
  const g = chain.gates[1];
  const signer = attesters[1];
  /** A SAS attestation account of our schema, encoded by `@solana/attestation`. */
  const encodeAccount = (fields: { expiry?: number; tokenAccount?: string } = {}) =>
    new Uint8Array(
      getAttestationEncoder().encode({
        discriminator: 2,
        nonce: address(g.nonce),
        credential: address(inputs.credential),
        schema: address(inputs.schema),
        data: fromHex(g.attestation_data),
        signer: address(signer.toBase58()),
        expiry: fields.expiry ?? 0,
        tokenAccount: address(fields.tokenAccount ?? PublicKey.default.toBase58()),
      }),
    );

  it("decodes every field at the §3.6 offsets", () => {
    const account = encodeAccount();
    expect(account).toHaveLength(246);
    expect(decodeGateAttestation(account)).toEqual({
      nonce: g.nonce,
      credential: inputs.credential,
      schema: inputs.schema,
      poHash: inputs.po_hash,
      gate: 1,
      evidenceHash: g.evidence_hash,
      signer: signer.toBase58(),
      expiry: BigInt(0),
    });
    expect(decodeGateAttestation(encodeAccount({ expiry: 1_760_000_060 })).expiry).toBe(
      BigInt(1_760_000_060),
    );
  });

  it("decodes a Buffer slice such as an RPC account's data", () => {
    const account = Buffer.concat([Buffer.alloc(7), Buffer.from(encodeAccount())]).subarray(7);
    expect(decodeGateAttestation(account).signer).toBe(signer.toBase58());
  });

  it("throws on each shape violation the program rejects as AttestationMalformed", () => {
    const account = encodeAccount();
    const forge = (offset: number, value: number) => {
      const forged = account.slice();
      forged[offset] = value;
      return forged;
    };
    const malformed = [
      account.subarray(1), // 245 bytes
      Uint8Array.of(...account, 0), // 247 bytes
      forge(0, 1), // a schema's discriminator
      forge(97, 72), // data length
      forge(101, 31), // po_hash length
      forge(138, 33), // evidence_hash length
      encodeAccount({ tokenAccount: relayer.toBase58() }), // tokenized
      forge(245, 1),
    ];
    for (const forged of malformed) {
      expect(() => decodeGateAttestation(forged)).toThrow(RangeError);
    }
  });
});
