// @vitest-environment node
import { createHash } from "node:crypto";

import { findAttestationPda, findCredentialPda, findSchemaPda } from "@solana/attestation";
import { address } from "@solana/kit";
import { Keypair, PublicKey } from "@solana/web3.js";
import { describe, expect, it } from "vitest";

import { SAS_CREDENTIAL_NAME, SAS_SCHEMA_NAME, SAS_SCHEMA_VERSION } from "../contracts";

import vectors from "../../tests/fixtures/vectors.json";
import { gateNonce } from "./hashes";
import {
  attestationPda,
  configPda,
  credentialPda,
  escrowPda,
  eventAuthorityPda,
  payoutAta,
  schemaPda,
  vaultAta,
} from "./pdas";

// The `chain` section of the golden vectors (spec 01 §3.10, written by `vectors.rs`).
const chain = vectors.chain;
const inputs = chain.inputs;

const pk = (base58: string) => new PublicKey(base58);
const kitAddress = (key: PublicKey) => address(key.toBase58());

/** The harness's throwaway `test_keypair(label)`: seed `sha256("bido-escrow-test:" ‖ label)`. */
function harnessPubkey(label: string): PublicKey {
  const seed = createHash("sha256").update(`bido-escrow-test:${label}`).digest();
  return Keypair.fromSeed(seed).publicKey;
}

const programId = pk(inputs.program_id);
const buyer = pk(inputs.buyer);
const mint = pk(inputs.mint);
const credential = pk(inputs.credential);
const schema = pk(inputs.schema);

describe("vectors inputs", () => {
  it("are the harness keys and the published program ids", () => {
    expect(buyer).toEqual(harnessPubkey("buyer"));
    expect(pk(inputs.payout)).toEqual(harnessPubkey("payout-owner"));
    expect(mint).toEqual(harnessPubkey("tusd-mint"));
    expect(inputs.sas_program_id).toBe("22zoJMtdu4tQc2PzL74ZUT7FrwgB1Udec8DdW4yw4BdG");
  });
});

describe("program PDAs", () => {
  it("configPda and eventAuthorityPda equal the vectors", () => {
    expect(configPda(programId).toBase58()).toBe(chain.config);
    expect(eventAuthorityPda(programId).toBase58()).toBe(chain.event_authority);
  });

  it("escrowPda returns the vectors' address and bump", () => {
    const [escrow, bump] = escrowPda(programId, buyer, inputs.chain_po_id);
    expect(escrow.toBase58()).toBe(chain.escrow);
    expect(bump).toBe(chain.escrow_bump);
  });

  it("vaultAta and payoutAta equal the vectors' associated token accounts", () => {
    expect(vaultAta(pk(chain.escrow), mint).toBase58()).toBe(chain.vault);
    expect(payoutAta(pk(inputs.payout), mint).toBase58()).toBe(chain.payout_token);
  });

  it("changes the address with every seed", () => {
    const [escrow] = escrowPda(programId, buyer, inputs.chain_po_id);
    expect(escrowPda(programId, buyer, "PO-1042-7KQ3")[0].equals(escrow)).toBe(false);
    expect(escrowPda(programId, pk(inputs.payout), inputs.chain_po_id)[0].equals(escrow)).toBe(
      false,
    );
    expect(escrowPda(pk(inputs.sas_program_id), buyer, inputs.chain_po_id)[0].equals(escrow)).toBe(
      false,
    );
    expect(configPda(pk(inputs.sas_program_id)).toBase58()).not.toBe(chain.config);
    expect(vaultAta(escrow, pk(inputs.payout)).toBase58()).not.toBe(chain.vault);
  });

  it("takes a chain_po_id of up to 32 bytes and rejects a longer one", () => {
    escrowPda(programId, buyer, `${"A".repeat(27)}-7KQ2`); // 32 bytes, the program's limit
    expect(() => escrowPda(programId, buyer, `${"A".repeat(28)}-7KQ2`)).toThrow(RangeError);
  });
});

describe("SAS PDAs", () => {
  it("credentialPda and schemaPda give the vectors' credential and schema of the harness issuer", () => {
    const derived = credentialPda(harnessPubkey("sas-issuer"), SAS_CREDENTIAL_NAME);
    expect(derived).toEqual(credential);
    expect(schemaPda(credential, SAS_SCHEMA_NAME, SAS_SCHEMA_VERSION)).toEqual(schema);
  });

  it("equal @solana/attestation's credential and schema PDAs", async () => {
    const issuer = harnessPubkey("sas-issuer");
    const [kitCredential] = await findCredentialPda({
      authority: kitAddress(issuer),
      name: SAS_CREDENTIAL_NAME,
    });
    expect(kitCredential).toBe(credentialPda(issuer, SAS_CREDENTIAL_NAME).toBase58());
    for (const version of [SAS_SCHEMA_VERSION, 2]) {
      const [kitSchema] = await findSchemaPda({
        credential: kitAddress(credential),
        name: SAS_SCHEMA_NAME,
        version,
      });
      expect(kitSchema).toBe(schemaPda(credential, SAS_SCHEMA_NAME, version).toBase58());
    }
  });

  it("attestationPda gives each gate's expected attestation from its nonce", async () => {
    const escrow = pk(chain.escrow);
    expect(chain.gates.map((g) => g.gate)).toEqual([0, 1]);
    for (const g of chain.gates) {
      const nonce = gateNonce(escrow, g.gate);
      expect(nonce).toEqual(pk(g.nonce).toBytes());
      expect(attestationPda(credential, schema, nonce).toBase58()).toBe(g.attestation);
      expect(attestationPda(credential, schema, pk(g.nonce)).toBase58()).toBe(g.attestation);
      const [kitAttestation] = await findAttestationPda({
        credential: kitAddress(credential),
        schema: kitAddress(schema),
        nonce: address(g.nonce),
      });
      expect(kitAttestation).toBe(g.attestation);
    }
  });

  it("binds the attestation to the credential, the schema and the nonce", () => {
    const [gate0, gate1] = chain.gates;
    const nonce = pk(gate0.nonce);
    expect(attestationPda(credential, schema, pk(gate1.nonce)).toBase58()).toBe(gate1.attestation);
    expect(attestationPda(schema, credential, nonce).toBase58()).not.toBe(gate0.attestation);
    const otherSchema = schemaPda(credential, SAS_SCHEMA_NAME, 2);
    expect(attestationPda(credential, otherSchema, nonce).toBase58()).not.toBe(gate0.attestation);
  });

  it("rejects a seed over 32 bytes, a version that is not a u8 and a nonce that is not 32 bytes", () => {
    expect(() => credentialPda(buyer, "x".repeat(33))).toThrow(RangeError);
    credentialPda(buyer, "x".repeat(32));
    for (const version of [-1, 256, 1.5]) {
      expect(() => schemaPda(credential, SAS_SCHEMA_NAME, version)).toThrow(RangeError);
    }
    const nonce = pk(chain.gates[0].nonce).toBytes();
    expect(() => attestationPda(credential, schema, nonce.subarray(1))).toThrow(RangeError);
  });
});
