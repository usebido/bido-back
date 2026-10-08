// @vitest-environment node
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";

import {
  ComputeBudgetProgram,
  Keypair,
  PublicKey,
  SystemProgram,
  TransactionMessage,
  VersionedTransaction,
  type TransactionInstruction,
} from "@solana/web3.js";
import { describe, expect, it } from "vitest";

import {
  CU_LIMITS,
  ESCROW_REFUND_AFTER_SECONDS,
  MAX_TX_BYTES,
  MEMO_PROGRAM_ID,
  MILESTONE_POLICY,
  SAS_PROGRAM_ID,
} from "../contracts";

import vectors from "../../tests/fixtures/vectors.json";
import { buildApprovalInstructions, buildCrankInstructions } from "./approval-tx";
import {
  ChainBuildError,
  createPoIx,
  decodePoEscrow,
  payoutRefMemoIx,
  releaseIx,
  type EscrowStatus,
  type MilestoneStatus,
  type PoEscrowAccount,
} from "./escrow";
import { decodeCreateAttestationData } from "./sas";

// The funded PO-1042 of the harness's fixture world (see `escrow.test.ts`): gates
// `[buyer, sqe]`, milestones 0 (gate 0), 1 and 2 (gate 1), all `Pending`.
const FUNDED = new Uint8Array(
  readFileSync(new URL("../../tests/fixtures/po_escrow_funded.bin", import.meta.url)),
);
/** Written by `chain-setup.ts smoke` (spec 01 §3.12) once it has run on devnet. */
const DEVNET_JSON = new URL("../../deployments/devnet.json", import.meta.url);

const TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
const ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";

const chain = vectors.chain;
const inputs = chain.inputs;
const pk = (base58: string) => new PublicKey(base58);
const programId = pk(inputs.program_id);
const escrow = pk(chain.escrow);

/** The harness's throwaway `test_keypair(label)`: seed `sha256("bido-escrow-test:" ‖ label)`. */
function harnessPubkey(label: string): PublicKey {
  const seed = createHash("sha256").update(`bido-escrow-test:${label}`).digest();
  return Keypair.fromSeed(seed).publicKey;
}

const buyer = harnessPubkey("buyer");
const sqe = harnessPubkey("sqe");
const relayer = harnessPubkey("relayer");
const hex = (bytes: Uint8Array) => Buffer.from(bytes).toString("hex");

function funded(): PoEscrowAccount {
  return decodePoEscrow(FUNDED);
}

/** The funded fixture with some milestones moved to another status. */
function withMilestones(statuses: Record<number, MilestoneStatus>, status?: EscrowStatus) {
  const state = funded();
  return {
    ...state,
    status: status ?? state.status,
    milestones: state.milestones.map((milestone, i) => ({
      ...milestone,
      status: statuses[i] ?? milestone.status,
    })),
  };
}

function approve(gate: number, approver: PublicKey, state = funded()): TransactionInstruction[] {
  const evidenceHash = chain.gates[gate].evidence_hash;
  return buildApprovalInstructions({
    programId,
    escrow,
    state,
    gate,
    approver,
    payer: relayer,
    evidenceHash,
  });
}

/** The code of the `ChainBuildError` that `build` throws. */
function thrownCode(build: () => unknown): string {
  try {
    build();
  } catch (error) {
    expect(error).toBeInstanceOf(ChainBuildError);
    return (error as ChainBuildError).code;
  }
  throw new Error("expected a ChainBuildError");
}

function expectRelease(ix: TransactionInstruction, milestone: number, state = funded()) {
  const expected = releaseIx({ programId, escrow, state, milestone });
  expect(ix.programId.toBase58()).toBe(programId.toBase58());
  expect(hex(ix.data)).toBe(hex(expected.data));
  expect(ix.keys.map((key) => key.pubkey.toBase58())).toEqual(
    expected.keys.map((key) => key.pubkey.toBase58()),
  );
}

describe("buildApprovalInstructions", () => {
  it.each([
    [0, buyer, [0]],
    [1, sqe, [1, 2]],
  ])(
    "gate %i is CreateAttestation then release of its pending milestones",
    (gate, approver, milestones) => {
      const [attest, ...releases] = approve(gate, approver);
      const g = chain.gates[gate];

      expect(attest.programId.toBase58()).toBe(SAS_PROGRAM_ID);
      expect(hex(attest.data)).toBe(g.create_attestation_data);
      expect(
        attest.keys.map(({ pubkey, isSigner, isWritable }) => [
          pubkey.toBase58(),
          isSigner,
          isWritable,
        ]),
      ).toEqual([
        [relayer.toBase58(), true, true],
        [approver.toBase58(), true, false],
        [inputs.credential, false, false],
        [inputs.schema, false, false],
        [g.attestation, false, true],
        [SystemProgram.programId.toBase58(), false, false],
      ]);
      const decoded = decodeCreateAttestationData(attest.data);
      expect(decoded.data).toEqual({ poHash: inputs.po_hash, gate, evidenceHash: g.evidence_hash });
      expect(decoded.expiry).toBe(BigInt(0));

      expect(releases).toHaveLength(milestones.length);
      releases.forEach((ix, i) => {
        expectRelease(ix, milestones[i]);
        // release reads the attestation the same transaction creates.
        expect(ix.keys[4].pubkey.toBase58()).toBe(g.attestation);
      });
    },
  );

  it("takes the evidence hash as bytes too", () => {
    const [attest] = buildApprovalInstructions({
      programId,
      escrow,
      state: funded(),
      gate: 1,
      approver: sqe,
      payer: relayer,
      evidenceHash: Buffer.from(chain.gates[1].evidence_hash, "hex"),
    });
    expect(hex(attest.data)).toBe(chain.gates[1].create_attestation_data);
  });

  it("releases only the gate's milestones that are still pending", () => {
    const state = withMilestones({ 1: "Released" });
    const [, ...releases] = approve(1, sqe, state);
    expect(releases).toHaveLength(1);
    expectRelease(releases[0], 2, state);
  });

  it("throws SIGN_SIGNER_MISMATCH unless the approver is the gate's attester", () => {
    expect(thrownCode(() => approve(0, sqe))).toBe("SIGN_SIGNER_MISMATCH");
    expect(thrownCode(() => approve(1, buyer))).toBe("SIGN_SIGNER_MISMATCH");
    expect(thrownCode(() => approve(1, relayer))).toBe("SIGN_SIGNER_MISMATCH");
  });

  it("throws ITEM_NOT_OPEN unless the escrow is Funded", () => {
    for (const status of ["Created", "Completed", "Refunded"] as const) {
      expect(thrownCode(() => approve(0, buyer, withMilestones({}, status)))).toBe("ITEM_NOT_OPEN");
    }
  });

  it("throws ITEM_NOT_OPEN when the gate has no pending milestone", () => {
    expect(thrownCode(() => approve(0, buyer, withMilestones({ 0: "Released" })))).toBe(
      "ITEM_NOT_OPEN",
    );
    const gate1Done = withMilestones({ 1: "Released", 2: "Released" });
    expect(thrownCode(() => approve(1, sqe, gate1Done))).toBe("ITEM_NOT_OPEN");
  });

  it("rejects a gate the escrow does not have", () => {
    for (const gate of [2, -1, 0.5]) {
      const build = () =>
        buildApprovalInstructions({
          programId,
          escrow,
          state: funded(),
          gate,
          approver: buyer,
          payer: relayer,
          evidenceHash: chain.gates[0].evidence_hash,
        });
      expect(build).toThrow(RangeError);
    }
  });
});

describe("buildCrankInstructions", () => {
  it("releases every pending milestone of the gate in ascending order", () => {
    const releases = buildCrankInstructions({ programId, escrow, state: funded(), gate: 1 });
    expect(releases).toHaveLength(2);
    expectRelease(releases[0], 1);
    expectRelease(releases[1], 2);
  });

  it("skips released and cancelled milestones and is empty when none is pending", () => {
    const state = withMilestones({ 1: "Released" });
    const releases = buildCrankInstructions({ programId, escrow, state, gate: 1 });
    expect(releases).toHaveLength(1);
    expectRelease(releases[0], 2, state);
    const refunded = withMilestones({ 0: "Released", 1: "Cancelled", 2: "Cancelled" }, "Refunded");
    for (const gate of [0, 1]) {
      expect(buildCrankInstructions({ programId, escrow, state: refunded, gate })).toEqual([]);
    }
  });

  it("rejects a gate the escrow does not have", () => {
    expect(() => buildCrankInstructions({ programId, escrow, state: funded(), gate: 2 })).toThrow(
      RangeError,
    );
  });
});

// ---- ESC-AC-12: the §3.9 transactions as v0 messages, with CB×2 and dummy signatures ----

/** `[SetComputeUnitLimit, SetComputeUnitPrice, …instructions]`, relayer as fee payer, no ALT. */
function v0Transaction(units: number, instructions: TransactionInstruction[]) {
  const message = new TransactionMessage({
    payerKey: relayer,
    recentBlockhash: PublicKey.default.toBase58(),
    instructions: [
      ComputeBudgetProgram.setComputeUnitLimit({ units }),
      ComputeBudgetProgram.setComputeUnitPrice({ microLamports: BigInt(1_000_000) }),
      ...instructions,
    ],
  }).compileToV0Message();
  // Without signatures, each required signer gets a zero (dummy) 64-byte signature.
  const tx = new VersionedTransaction(message);
  return {
    bytes: tx.serialize().length,
    signers: message.header.numRequiredSignatures,
    keys: message.staticAccountKeys.map((key) => key.toBase58()).sort(),
  };
}

const sorted = (keys: (PublicKey | string)[]) => keys.map((key) => key.toString()).sort();

describe("transaction size (§3.9, ESC-AC-12)", () => {
  it("approve_ppap fits in a packet with the 15 keys of §3.9", () => {
    const tx = v0Transaction(CU_LIMITS.approval, approve(1, sqe));
    expect(tx.signers).toBe(2);
    expect(tx.keys).toEqual(
      sorted([
        relayer,
        sqe,
        ComputeBudgetProgram.programId,
        SAS_PROGRAM_ID,
        inputs.credential,
        inputs.schema,
        chain.gates[1].attestation,
        SystemProgram.programId,
        programId,
        escrow,
        chain.vault,
        chain.payout_token,
        inputs.mint,
        TOKEN_PROGRAM,
        chain.event_authority,
      ]),
    );
    expect(tx.bytes).toBeLessThanOrEqual(MAX_TX_BYTES);
  });

  it("release_tool_start fits in a packet", () => {
    const tx = v0Transaction(CU_LIMITS.approval, approve(0, buyer));
    expect(tx.signers).toBe(2);
    expect(tx.bytes).toBeLessThanOrEqual(MAX_TX_BYTES);
  });

  it("create_po with its payout_ref Memo fits in a packet with the 17 keys of §3.9", () => {
    const operator = harnessPubkey("operator");
    const createPo = createPoIx({
      programId,
      operator,
      payer: relayer,
      buyer: pk(inputs.buyer),
      payout: pk(inputs.payout),
      mint: pk(inputs.mint),
      credential: pk(inputs.credential),
      args: {
        // The longest id the program takes, so the size bound holds for every PO.
        chainPoId: `${"A".repeat(27)}-7KQ2`,
        poHash: inputs.po_hash,
        cadHash: "ca".repeat(32),
        total: BigInt(1_200_000_000_000),
        gateAttesters: [buyer, sqe],
        milestones: MILESTONE_POLICY.map(({ name, bps, gate }) => ({ name, bps, gate })),
        refundAfter: BigInt(1_791_464_400 + ESCROW_REFUND_AFTER_SECONDS),
      },
    });
    const tx = v0Transaction(CU_LIMITS.createPo, [createPo, payoutRefMemoIx("9e".repeat(32))]);
    expect(tx.signers).toBe(2);
    expect(tx.keys).toHaveLength(17);
    expect(tx.keys).toEqual(
      expect.arrayContaining(
        sorted([relayer, operator, ATA_PROGRAM, MEMO_PROGRAM_ID, chain.config, inputs.buyer]),
      ),
    );
    expect(tx.bytes).toBeLessThanOrEqual(MAX_TX_BYTES);
  });
});

// ---- ESC-AC-12: CU_LIMITS ≥ 1.3 × the devnet measurements, once there are any ----

type CuKind = keyof typeof CU_LIMITS;

/** The kinds whose limit is below 1.3 × the measured maximum (a measurement of 0 = not measured). */
function cuLimitShortfalls(
  limits: Record<CuKind, number>,
  measured: Partial<Record<string, number>>,
): CuKind[] {
  return (Object.keys(limits) as CuKind[]).filter((kind) => {
    const cu = measured[kind] ?? 0;
    return cu > 0 && limits[kind] * 10 < cu * 13;
  });
}

describe("CU_LIMITS", () => {
  it("flags a limit below 1.3 × its measurement and ignores unmeasured kinds", () => {
    expect(cuLimitShortfalls(CU_LIMITS, {})).toEqual([]);
    expect(cuLimitShortfalls(CU_LIMITS, { fund: 0, approval: 0 })).toEqual([]);
    expect(cuLimitShortfalls(CU_LIMITS, { fund: 46_153, release: 61_539 })).toEqual(["release"]);
    expect(cuLimitShortfalls(CU_LIMITS, { createPo: 250_000 })).toEqual(["createPo"]);
  });

  it("are at least 1.3 × each non-zero measurement in deployments/devnet.json", () => {
    const measured = existsSync(DEVNET_JSON)
      ? ((
          JSON.parse(readFileSync(DEVNET_JSON, "utf8")) as {
            measurements?: { cu?: Partial<Record<string, number>> };
          }
        ).measurements?.cu ?? {})
      : {};
    expect(cuLimitShortfalls(CU_LIMITS, measured)).toEqual([]);
  });
});
