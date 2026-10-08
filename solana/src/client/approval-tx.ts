// The program instructions of an approval and of the release crank (spec 01 §3.9, §3.11): SAS
// `CreateAttestation` for the gate, then `release(m)` for every `Pending` milestone of that gate in
// ascending index order. Whoever assembles the transaction prepends exactly
// `[SetComputeUnitLimit, SetComputeUnitPrice]` (spec 02); nothing else is added. Twin of
// `bido_back.chain.codec.approval_tx`.

import { PublicKey, type TransactionInstruction } from "@solana/web3.js";

import { ChainBuildError, releaseIx, type Gate, type PoEscrowAccount } from "./escrow";
import { gateNonce } from "./hashes";
import { createAttestationIx, encodeGateApprovalData } from "./sas";

/**
 * `[CreateAttestation(gate), release(m)…]` for the approval of `gate` by `approver`, the relayer
 * `payer` paying the attestation's rent. Throws `ChainBuildError("SIGN_SIGNER_MISMATCH")` unless
 * `approver` is the gate's pinned attester, and `ChainBuildError("ITEM_NOT_OPEN")` unless the escrow
 * is `Funded` and the gate has a `Pending` milestone. `evidenceHash` is
 * `computeEvidenceHash(chainPoId, gate, fileHashes)`: 32 bytes or 64 lowercase hex digits.
 */
export function buildApprovalInstructions({
  programId,
  escrow,
  state,
  gate,
  approver,
  payer,
  evidenceHash,
}: {
  programId: PublicKey;
  escrow: PublicKey;
  state: PoEscrowAccount;
  gate: number;
  approver: PublicKey;
  payer: PublicKey;
  evidenceHash: Uint8Array | string;
}): TransactionInstruction[] {
  const { attester } = gateOf(state, gate);
  if (approver.toBase58() !== attester) {
    throw new ChainBuildError(
      "SIGN_SIGNER_MISMATCH",
      `${approver.toBase58()} is not the attester of gate ${gate} (${attester})`,
    );
  }
  if (state.status !== "Funded") {
    throw new ChainBuildError("ITEM_NOT_OPEN", `escrow is ${state.status}, not Funded`);
  }
  const milestones = pendingMilestones(state, gate);
  if (milestones.length === 0) {
    throw new ChainBuildError("ITEM_NOT_OPEN", `gate ${gate} has no pending milestone`);
  }
  const attestation = createAttestationIx({
    payer,
    signer: approver,
    credential: new PublicKey(state.credential),
    schema: new PublicKey(state.schema),
    nonce: gateNonce(escrow, gate),
    data: encodeGateApprovalData({ poHash: state.poHash, gate, evidenceHash }),
  });
  return [
    attestation,
    ...milestones.map((milestone) => releaseIx({ programId, escrow, state, milestone })),
  ];
}

/**
 * `[release(m)…]` for every `Pending` milestone of `gate`, against the gate's existing attestation
 * (the `release_attempt` crank, recovery only); empty when none is pending.
 */
export function buildCrankInstructions({
  programId,
  escrow,
  state,
  gate,
}: {
  programId: PublicKey;
  escrow: PublicKey;
  state: PoEscrowAccount;
  gate: number;
}): TransactionInstruction[] {
  gateOf(state, gate);
  return pendingMilestones(state, gate).map((milestone) =>
    releaseIx({ programId, escrow, state, milestone }),
  );
}

function gateOf(state: PoEscrowAccount, gate: number): Gate {
  if (!Number.isInteger(gate) || gate < 0 || gate >= state.gates.length) {
    throw new RangeError(`escrow has ${state.gates.length} gates, not gate ${gate}`);
  }
  return state.gates[gate];
}

/** Indexes of the gate's `Pending` milestones, ascending. */
function pendingMilestones(state: PoEscrowAccount, gate: number): number[] {
  return state.milestones.flatMap((milestone, index) =>
    milestone.gate === gate && milestone.status === "Pending" ? [index] : [],
  );
}
