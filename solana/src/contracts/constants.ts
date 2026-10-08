// Extracted on-chain constants from the payment MVP contracts.

export const CHAIN_PO_ID_RE = "^[A-Z0-9-]{1,27}-[0-9A-HJKMNP-TV-Z]{4}$" as const;
export const CIRCLE_DEVNET_USDC_MINT = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU" as const;
export const CU_LIMITS = {
  approval: 200000,
  createPo: 250000,
  fund: 60000,
  refund: 80000,
  release: 80000,
} as const;
export const ESCROW_REFUND_AFTER_SECONDS = 7776000 as const;
export const GATE_NONCE_DOMAIN = "bido-gate-v1" as const;
export const MAX_CHAIN_PO_ID_LEN = 32 as const;
export const MAX_GATES = 4 as const;
export const MAX_MILESTONES = 4 as const;
export const MAX_MILESTONE_NAME_LEN = 24 as const;
export const MAX_TX_BYTES = 1232 as const;
export const MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr" as const;
export const MILESTONE_POLICY = [
  { bps: 3000, gate: 0, name: "Tool start" },
  { bps: 3000, gate: 1, name: "T1" },
  { bps: 4000, gate: 1, name: "PPAP" },
] as const;
export const SAS_CREDENTIAL_NAME = "bido-approvals" as const;
export const SAS_PROGRAM_ID = "22zoJMtdu4tQc2PzL74ZUT7FrwgB1Udec8DdW4yw4BdG" as const;
export const SAS_SCHEMA_FIELDS = ["po_hash", "gate", "evidence_hash"] as const;
export const SAS_SCHEMA_LAYOUT = [13, 0, 13] as const;
export const SAS_SCHEMA_NAME = "bido-gate-approval" as const;
export const SAS_SCHEMA_VERSION = 1 as const;
