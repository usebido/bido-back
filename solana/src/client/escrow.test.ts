// @vitest-environment node
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

import { getAssociatedTokenAddressSync } from "@solana/spl-token";
import { Keypair, PublicKey, type TransactionInstruction } from "@solana/web3.js";
import { describe, expect, it } from "vitest";

import {
  CIRCLE_DEVNET_USDC_MINT,
  ESCROW_REFUND_AFTER_SECONDS,
  MILESTONE_POLICY,
} from "../contracts";

import vectors from "../../tests/fixtures/vectors.json";
import {
  ChainBuildError,
  createPoIx,
  decodeConfig,
  decodePoEscrow,
  escrowCoder,
  fundIx,
  payoutRefMemoIx,
  refundIx,
  releaseIx,
  type CreatePoArgs,
  type PoEscrowAccount,
} from "./escrow";

// The funded PO-1042 account that `events.rs` dumps from the harness's fixture world (`Clock` at
// NOW, hashes 0x50.. and 0xCA.., $1.2M split 30/30/40 between two gates).
const FUNDED = new Uint8Array(
  readFileSync(new URL("../../tests/fixtures/po_escrow_funded.bin", import.meta.url)),
);
const NOW = BigInt(1_791_464_400);
const TOTAL = BigInt(1_200_000_000_000);
const AMOUNTS = [BigInt(360_000_000_000), BigInt(360_000_000_000), BigInt(480_000_000_000)];
const PO_HASH = new Uint8Array(32).fill(0x50);
const CAD_HASH = new Uint8Array(32).fill(0xca);

const TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
const ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";
const SYSTEM_PROGRAM = "11111111111111111111111111111111";
const MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr";

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

/** An account meta with its address in base58, so a failing comparison shows addresses. */
interface Meta {
  pubkey: string;
  isSigner: boolean;
  isWritable: boolean;
}

function meta(pubkey: PublicKey | string, { signer = false, writable = false } = {}): Meta {
  return { pubkey: pubkey.toString(), isSigner: signer, isWritable: writable };
}

function keysOf(ix: TransactionInstruction): Meta[] {
  return ix.keys.map(({ pubkey, isSigner, isWritable }) =>
    meta(pubkey, { signer: isSigner, writable: isWritable }),
  );
}

/** Anchor's instruction discriminator `sha256("global:<name>")[..8]`. */
function discriminator(name: string): Buffer {
  return createHash("sha256").update(`global:${name}`).digest().subarray(0, 8);
}

function u16(value: number): Buffer {
  const out = Buffer.alloc(2);
  out.writeUInt16LE(value);
  return out;
}

function u32(value: number): Buffer {
  const out = Buffer.alloc(4);
  out.writeUInt32LE(value);
  return out;
}

function i64(value: bigint): Buffer {
  const out = Buffer.alloc(8);
  out.writeBigInt64LE(value);
  return out;
}

const hex = (bytes: Uint8Array) => Buffer.from(bytes).toString("hex");
const funded = () => decodePoEscrow(FUNDED);

function expectedFunded(): PoEscrowAccount {
  const attesters = [harnessPubkey("buyer"), harnessPubkey("sqe")];
  return {
    chainPoId: "PO-1042-7KQ2",
    buyer: inputs.buyer,
    payout: inputs.payout,
    payoutToken: chain.payout_token,
    mint: inputs.mint,
    vault: chain.vault,
    poHash: hex(PO_HASH),
    cadHash: hex(CAD_HASH),
    total: TOTAL,
    released: BigInt(0),
    status: "Funded",
    credential: inputs.credential,
    schema: inputs.schema,
    gates: chain.gates.map((g, i) => ({
      attester: attesters[i].toBase58(),
      attestation: g.attestation,
    })),
    milestones: MILESTONE_POLICY.map((term, i) => ({
      name: term.name,
      bps: term.bps,
      amount: AMOUNTS[i],
      gate: term.gate,
      status: "Pending",
      releasedSlot: BigInt(0),
      approvedBy: SYSTEM_PROGRAM,
      evidenceHash: "00".repeat(32),
    })),
    refundAfter: NOW + BigInt(ESCROW_REFUND_AFTER_SECONDS),
    bump: chain.escrow_bump,
  };
}

describe("decodePoEscrow", () => {
  it("reads the funded fixture into the camelCase fields of §3.4", () => {
    expect(inputs.po_hash).toBe(hex(PO_HASH));
    expect(funded()).toEqual(expectedFunded());
  });

  it("agrees with escrowCoder, which re-encodes the fixture byte for byte", async () => {
    const raw = escrowCoder.accounts.decode("PoEscrow", Buffer.from(FUNDED));
    const encoded = await escrowCoder.accounts.encode("PoEscrow", raw);
    // The account is allocated at its maximum size; the tail after the value is zero.
    expect(FUNDED.length).toBe(1070);
    expect(hex(FUNDED)).toBe(hex(Buffer.concat([encoded, Buffer.alloc(1070 - encoded.length)])));
  });

  it("rejects another account, a truncated one and an unknown status", () => {
    expect(() => decodeConfig(FUNDED)).toThrow(/discriminator/);
    expect(() => decodePoEscrow(FUNDED.subarray(0, 200))).toThrow();
    const forged = FUNDED.slice();
    const statusOffset = 8 + 4 + "PO-1042-7KQ2".length + 5 * 32 + 2 * 32 + 2 * 8;
    expect(forged[statusOffset]).toBe(1); // Funded
    forged[statusOffset] = 4;
    expect(() => decodePoEscrow(forged)).toThrow();
  });
});

describe("decodeConfig", () => {
  it("reads a coder-encoded Config, ignoring the zero tail", async () => {
    const data = await escrowCoder.accounts.encode("Config", {
      admin: harnessPubkey("admin"),
      operator: harnessPubkey("operator"),
      allowed_mints: [pk(inputs.mint), pk(CIRCLE_DEVNET_USDC_MINT)],
      sas_credential: pk(inputs.credential),
      sas_schema: pk(inputs.schema),
      bump: chain.config_bump,
    });
    expect(decodeConfig(Buffer.concat([data, Buffer.alloc(10)]))).toEqual({
      admin: harnessPubkey("admin").toBase58(),
      operator: harnessPubkey("operator").toBase58(),
      allowedMints: [inputs.mint, CIRCLE_DEVNET_USDC_MINT],
      sasCredential: inputs.credential,
      sasSchema: inputs.schema,
      bump: chain.config_bump,
    });
  });
});

function createPoArgs(overrides: Partial<CreatePoArgs> = {}): CreatePoArgs {
  return {
    chainPoId: "PO-1042-7KQ2",
    poHash: PO_HASH,
    cadHash: CAD_HASH,
    total: TOTAL,
    gateAttesters: [harnessPubkey("buyer"), harnessPubkey("sqe")],
    milestones: MILESTONE_POLICY.map(({ name, bps, gate }) => ({ name, bps, gate })),
    refundAfter: NOW + BigInt(ESCROW_REFUND_AFTER_SECONDS),
    ...overrides,
  };
}

function buildCreatePo(args: CreatePoArgs) {
  return createPoIx({
    programId,
    operator: harnessPubkey("operator"),
    payer: harnessPubkey("relayer"),
    buyer: pk(inputs.buyer),
    payout: pk(inputs.payout),
    mint: pk(inputs.mint),
    credential: pk(inputs.credential),
    args,
  });
}

describe("createPoIx", () => {
  it("lists the §3.5 accounts with the IDL's flags and encodes the arguments", () => {
    const ix = buildCreatePo(createPoArgs());
    expect(ix.programId.toBase58()).toBe(programId.toBase58());
    expect(keysOf(ix)).toEqual([
      meta(chain.config),
      meta(harnessPubkey("operator"), { signer: true }),
      meta(harnessPubkey("relayer"), { signer: true, writable: true }),
      meta(inputs.buyer),
      meta(inputs.payout),
      meta(inputs.mint),
      meta(chain.escrow, { writable: true }),
      meta(chain.vault, { writable: true }),
      meta(chain.payout_token, { writable: true }),
      meta(inputs.credential),
      meta(TOKEN_PROGRAM),
      meta(ATA_PROGRAM),
      meta(SYSTEM_PROGRAM),
      meta(chain.event_authority),
      meta(programId),
    ]);
    const milestones = MILESTONE_POLICY.map((term) =>
      Buffer.concat([
        u32(term.name.length),
        Buffer.from(term.name),
        u16(term.bps),
        Buffer.of(term.gate),
      ]),
    );
    const total = Buffer.alloc(8);
    total.writeBigUInt64LE(TOTAL);
    expect(hex(ix.data)).toBe(
      hex(
        Buffer.concat([
          discriminator("create_po"),
          u32(12),
          Buffer.from("PO-1042-7KQ2"),
          PO_HASH,
          CAD_HASH,
          total,
          u32(2),
          harnessPubkey("buyer").toBuffer(),
          harnessPubkey("sqe").toBuffer(),
          u32(3),
          ...milestones,
          i64(NOW + BigInt(ESCROW_REFUND_AFTER_SECONDS)),
        ]),
      ),
    );
  });

  it("takes the hashes as lowercase hex too, and escrowCoder decodes the data back", () => {
    const fromHex = buildCreatePo(createPoArgs({ poHash: hex(PO_HASH), cadHash: hex(CAD_HASH) }));
    const ix = buildCreatePo(createPoArgs());
    expect(hex(fromHex.data)).toBe(hex(ix.data));
    const decoded = escrowCoder.instruction.decode(ix.data);
    expect(decoded?.name).toBe("create_po");
    const { args } = decoded?.data as { args: Record<string, unknown> };
    expect(args.chain_po_id).toBe("PO-1042-7KQ2");
    expect(String(args.total)).toBe(TOTAL.toString());
    expect(args.milestones).toEqual(
      MILESTONE_POLICY.map(({ name, bps, gate }) => ({ name, bps, gate })),
    );
  });

  it("rejects arguments the program cannot take", () => {
    expect(() => buildCreatePo(createPoArgs({ chainPoId: `${"A".repeat(28)}-7KQ2` }))).toThrow(
      /longer than 32 bytes/,
    );
    expect(() => buildCreatePo(createPoArgs({ poHash: new Uint8Array(31) }))).toThrow(
      /poHash must be 32 bytes/,
    );
    expect(() => buildCreatePo(createPoArgs({ cadHash: "CA".repeat(32) }))).toThrow(
      /cadHash must be 64 lowercase hex/,
    );
    for (const total of [BigInt(-1), BigInt(2) ** BigInt(64)]) {
      expect(() => buildCreatePo(createPoArgs({ total }))).toThrow(/does not fit in u64/);
    }
    expect(() => buildCreatePo(createPoArgs({ refundAfter: BigInt(2) ** BigInt(63) }))).toThrow(
      /does not fit in i64/,
    );
    const milestones = createPoArgs().milestones;
    for (const bad of [{ bps: 65_536 }, { bps: 0.5 }, { gate: 256 }, { gate: -1 }]) {
      const withBad = [{ ...milestones[0], ...bad }, ...milestones.slice(1)];
      expect(() => buildCreatePo(createPoArgs({ milestones: withBad }))).toThrow(RangeError);
    }
  });
});

const eventAuthority = () => meta(chain.event_authority);

describe("fundIx", () => {
  it("moves the buyer's tokens into the vault, signed by the buyer", () => {
    const state = funded();
    const buyerToken = getAssociatedTokenAddressSync(pk(state.mint), pk(state.buyer));
    const ix = fundIx({ programId, escrow, state, buyerToken });
    expect(ix.programId.toBase58()).toBe(programId.toBase58());
    expect(keysOf(ix)).toEqual([
      meta(state.buyer, { signer: true }),
      meta(escrow, { writable: true }),
      meta(buyerToken, { writable: true }),
      meta(state.vault, { writable: true }),
      meta(state.mint),
      meta(TOKEN_PROGRAM),
      eventAuthority(),
      meta(programId),
    ]);
    expect(hex(ix.data)).toBe(hex(discriminator("fund")));
  });
});

function releaseAccounts(state: PoEscrowAccount, attestation: PublicKey | string): Meta[] {
  return [
    meta(escrow, { writable: true }),
    meta(state.vault, { writable: true }),
    meta(state.payoutToken, { writable: true }),
    meta(state.mint),
    meta(attestation),
    meta(TOKEN_PROGRAM),
    eventAuthority(),
    meta(programId),
  ];
}

describe("releaseIx", () => {
  it.each([
    [0, 0],
    [1, 1],
    [2, 1],
  ])("release(%i) reads the attestation of gate %i", (milestone, gate) => {
    const state = funded();
    const ix = releaseIx({ programId, escrow, state, milestone });
    expect(keysOf(ix)).toEqual(releaseAccounts(state, chain.gates[gate].attestation));
    expect(hex(ix.data)).toBe(hex(Buffer.concat([discriminator("release"), Buffer.of(milestone)])));
  });

  it("takes an attestation override for the failed-release scene", () => {
    const state = funded();
    const foreign = harnessPubkey("foreign-attestation");
    const ix = releaseIx({ programId, escrow, state, milestone: 2, attestation: foreign });
    expect(keysOf(ix)).toEqual(releaseAccounts(state, foreign));
    expect(hex(ix.data)).toBe(hex(Buffer.concat([discriminator("release"), Buffer.of(2)])));
  });

  it("rejects a milestone the escrow does not have", () => {
    for (const milestone of [3, -1, 1.5]) {
      expect(() => releaseIx({ programId, escrow, state: funded(), milestone })).toThrow(
        /has 3 milestones/,
      );
    }
  });
});

describe("refundIx", () => {
  it("pays back the derived buyer ATA, signed by the buyer", () => {
    const state = funded();
    const ix = refundIx({ programId, escrow, state });
    expect(keysOf(ix)).toEqual([
      meta(state.buyer, { signer: true }),
      meta(escrow, { writable: true }),
      meta(state.vault, { writable: true }),
      meta(getAssociatedTokenAddressSync(pk(state.mint), pk(state.buyer)), { writable: true }),
      meta(state.mint),
      meta(TOKEN_PROGRAM),
      eventAuthority(),
      meta(programId),
    ]);
    expect(hex(ix.data)).toBe(hex(discriminator("refund")));
  });
});

describe("payoutRefMemoIx", () => {
  it("is the 80-byte Memo bido:payout_ref:<hex> with no accounts", () => {
    const ix = payoutRefMemoIx("9e".repeat(32));
    expect(ix.programId.toBase58()).toBe(MEMO_PROGRAM);
    expect(keysOf(ix)).toEqual([]);
    expect(ix.data.toString("utf8")).toBe(`bido:payout_ref:${"9e".repeat(32)}`);
    expect(ix.data.length).toBe(80);
  });

  it("rejects anything but 64 lowercase hex digits", () => {
    for (const bad of ["9E".repeat(32), "9e".repeat(31), "9e".repeat(33), "zz".repeat(32), ""]) {
      expect(() => payoutRefMemoIx(bad)).toThrow(/64 lowercase hex/);
    }
  });
});

describe("ChainBuildError", () => {
  it("carries an AppErrorCode", () => {
    const error = new ChainBuildError("ITEM_NOT_OPEN", "gate 1 has no pending milestone");
    expect(error).toBeInstanceOf(Error);
    expect(error.name).toBe("ChainBuildError");
    expect(error.code).toBe("ITEM_NOT_OPEN");
    expect(error.message).toBe("gate 1 has no pending milestone");
    expect(new ChainBuildError("SIGN_SIGNER_MISMATCH").message).toBe("SIGN_SIGNER_MISMATCH");
  });
});
