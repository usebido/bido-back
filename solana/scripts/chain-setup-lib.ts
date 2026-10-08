/**
 * The pure part of `chain-setup.ts` (spec 01 §3.12): argument parsing, the devnet and key-path
 * refusals, `deployments/devnet.json` parsing and writing, the account decoders, each step's
 * idempotence planner over a `ChainReader`, and the instruction builders. Nothing here reads env,
 * key files or the network: `chain-setup.ts` does, and `chain-setup.test.ts` drives this file on a
 * fake chain.
 */
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import {
  getChangeAuthorizedSignersInstruction,
  getCreateCredentialInstruction,
  getCreateSchemaInstruction,
  getCredentialDecoder,
  getSchemaDecoder,
} from "@solana/attestation";
import { AccountRole, address, createNoopSigner } from "@solana/kit";
import {
  ASSOCIATED_TOKEN_PROGRAM_ID,
  AuthorityType,
  MINT_SIZE,
  TOKEN_PROGRAM_ID,
  createAssociatedTokenAccountIdempotentInstruction,
  createInitializeMint2Instruction,
  createMintToCheckedInstruction,
  createSetAuthorityInstruction,
  createTransferCheckedInstruction,
  getAssociatedTokenAddressSync,
  unpackAccount,
  unpackMint,
} from "@solana/spl-token";
import {
  AddressLookupTableAccount,
  AddressLookupTableProgram,
  ComputeBudgetProgram,
  Keypair,
  PublicKey,
  SystemProgram,
  TransactionInstruction,
} from "@solana/web3.js";

import {
  CIRCLE_DEVNET_USDC_MINT,
  CU_LIMITS,
  ESCROW_REFUND_AFTER_SECONDS,
  MAX_TX_BYTES,
  MILESTONE_POLICY,
  SAS_CREDENTIAL_NAME,
  SAS_PROGRAM_ID,
  SAS_SCHEMA_FIELDS,
  SAS_SCHEMA_LAYOUT,
  SAS_SCHEMA_NAME,
  SAS_SCHEMA_VERSION,
} from "../src/contracts";
import {
  decodeConfig,
  escrowCoder,
  type ConfigAccount,
  type CreatePoArgs,
  type PoEscrowAccount,
} from "../src/client/escrow";
import { newChainPoId } from "../src/client/ids";
import { sha256 } from "../src/client/hashes";
import {
  configPda,
  credentialPda,
  eventAuthorityPda,
  payoutAta,
  schemaPda,
} from "../src/client/pdas";

// ---- constants ----

export const DEVNET_GENESIS_HASH = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";
export const PUBLIC_DEVNET_RPC_URL = "https://api.devnet.solana.com";
export const EXPLORER_TX_URL = "https://explorer.solana.com/tx/";

/** Bido Test USD (devnet): classic SPL Token, 6 decimals, 1,000,000,000 tUSD minted once. */
export const TUSD_DECIMALS = 6;
export const TUSD_SUPPLY = BigInt("1000000000000000");
/** Circle devnet USDC has 6 decimals too. */
export const CIRCLE_DECIMALS = 6;

export const SAS_SCHEMA_DESCRIPTION =
  "Approval of a PO gate; releases escrowed funds to the fixed payout";
export const PAYOUT_OWNER_LABEL = "Tazapay deposit (devnet stand-in)";

/** `keys:check` fails when the deploy payer or the relayer holds less. */
export const MIN_SOL_LAMPORTS = 2_000_000_000;
/** Spec 02's `CU_PRICE_FLOOR` (micro-lamports), the price every setup transaction offers. */
export const SETUP_CU_PRICE_MICRO_LAMPORTS = 1_000;

/** Key files in `BIDO_KEYS_DIR`, without `.json` (spec 01 §3.2). */
export const KEY_NAMES = [
  "admin",
  "deploy-payer",
  "operator",
  "relayer",
  "smoke-gate0",
  "smoke-gate1",
  "smoke-buyer",
  "sas-issuer",
  "treasury",
  "payout-owner",
] as const;
export type KeyName = (typeof KEY_NAMES)[number];
/** Keys `keys:check` requires, with at least `MIN_SOL_LAMPORTS` each. */
export const FUNDED_KEYS: readonly KeyName[] = ["deploy-payer", "relayer"];

/** The smoke POs: `SMOKE-<suffix>`, 100 tUSD or (P1) 12 Circle devnet USDC, the demo milestones. */
export const SMOKE_PO_NUMBER = "SMOKE";
export const SMOKE_TOTALS = {
  tusd: BigInt(100_000_000),
  circle: BigInt(12_000_000),
} as const;

const UPGRADEABLE_LOADER_ID = "BPFLoaderUpgradeab1e11111111111111111111111";
/** `UpgradeableLoaderState::ProgramData` header: u32 tag, u64 slot, option tag, authority. */
const PROGRAM_DATA_HEADER_LEN = 45;
const LOADER_TAG_PROGRAM = 2;
const LOADER_TAG_PROGRAM_DATA = 3;
const SAS_CREDENTIAL_DISC = 0;
const SAS_SCHEMA_DISC = 1;

// ---- errors ----

/** A refusal or failed check, printed without a stack and exiting 1. */
export class SetupError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SetupError";
  }
}

/** A bad command line, printed with the usage and exiting 2. */
export class UsageError extends SetupError {
  constructor(message: string) {
    super(message);
    this.name = "UsageError";
  }
}

// ---- arguments ----

export const USAGE = `usage: pnpm tsx scripts/chain-setup.ts <step> [flags]

steps:
  keys:check                              public keys and SOL balances
  mint                                    create or check Bido Test USD (treasury.json)
  sas [--signers <pk,pk>]                 credential "${SAS_CREDENTIAL_NAME}" and schema "${SAS_SCHEMA_NAME}" (sas-issuer.json)
  sas:signers --set <pk,pk>               set the credential's signers to exactly this list (sas-issuer.json)
  config                                  init_config or update_config (admin.json)
  payout --owner <pubkey>                 record the payout owner and create its tUSD account
  topup --to <pubkey> --amount <tUSD>     bring an account up to at least this many tUSD (treasury.json)
  smoke [--only gate0] [--mint tusd|circle] [--po <chain_po_id>]
                                          create, fund and approve smoke escrows on devnet
  alt                                     address lookup table, only if a transaction is too large
  verify [--recording]                    re-read deployments/devnet.json from chain and check it

A signer list item is <pubkey> or <label>=<pubkey>.
env: SOLANA_RPC_URL (devnet only), BIDO_KEYS_DIR (outside the repository)`;

export interface SignerEntry {
  pubkey: string;
  label?: string;
}

export type Command =
  | { step: "help" }
  | { step: "keys:check" }
  | { step: "mint" }
  | { step: "sas"; signers?: SignerEntry[] }
  | { step: "sas:signers"; set: SignerEntry[] }
  | { step: "config" }
  | { step: "payout"; owner: string }
  | { step: "topup"; to: string; amount: bigint }
  | { step: "smoke"; only?: "gate0"; mint: "tusd" | "circle"; po?: string }
  | { step: "alt" }
  | { step: "verify"; recording: boolean };

type StepName = Exclude<Command["step"], "help">;

/** Value flags and boolean flags each step accepts. */
const STEP_FLAGS: Record<StepName, { values: string[]; booleans: string[] }> = {
  "keys:check": { values: [], booleans: [] },
  mint: { values: [], booleans: [] },
  sas: { values: ["--signers"], booleans: [] },
  "sas:signers": { values: ["--set"], booleans: [] },
  config: { values: [], booleans: [] },
  payout: { values: ["--owner"], booleans: [] },
  topup: { values: ["--to", "--amount"], booleans: [] },
  smoke: { values: ["--only", "--mint", "--po"], booleans: [] },
  alt: { values: [], booleans: [] },
  verify: { values: [], booleans: ["--recording"] },
};

/** Parses `<step> [flags]`; throws `UsageError` on anything unknown, missing or malformed. */
export function parseArgs(argv: readonly string[]): Command {
  const [step, ...rest] = argv;
  if (step === undefined || step === "--help" || step === "-h" || step === "help") {
    return { step: "help" };
  }
  if (!(step in STEP_FLAGS)) throw new UsageError(`unknown step "${step}"`);
  const name = step as StepName;
  if (name === "mint" && rest.includes("--metadata")) {
    throw new UsageError(
      "--metadata is not available: the founder chose an unnamed tUSD mint (IMPLEMENTATION_PLAN.md §8, decision 3)",
    );
  }
  const { values, booleans } = parseFlags(name, rest);
  switch (name) {
    case "keys:check":
    case "mint":
    case "config":
    case "alt":
      return { step: name };
    case "sas": {
      const signers = values.get("--signers");
      return signers === undefined
        ? { step: "sas" }
        : { step: "sas", signers: parseSignerList(signers, "--signers") };
    }
    case "sas:signers":
      return { step: "sas:signers", set: parseSignerList(required(values, "--set"), "--set") };
    case "payout":
      return { step: "payout", owner: parsePubkey(required(values, "--owner"), "--owner") };
    case "topup":
      return {
        step: "topup",
        to: parsePubkey(required(values, "--to"), "--to"),
        amount: parseTokenAmount(required(values, "--amount"), "--amount", TUSD_DECIMALS),
      };
    case "smoke": {
      const only = values.get("--only");
      if (only !== undefined && only !== "gate0") {
        throw new UsageError(`--only takes "gate0", got "${only}"`);
      }
      const mint = values.get("--mint") ?? "tusd";
      if (mint !== "tusd" && mint !== "circle") {
        throw new UsageError(`--mint takes "tusd" or "circle", got "${mint}"`);
      }
      const po = values.get("--po");
      if (po !== undefined) assertSmokePoId(po);
      return {
        step: "smoke",
        mint,
        ...(only === undefined ? {} : { only }),
        ...(po === undefined ? {} : { po }),
      };
    }
    case "verify":
      return { step: "verify", recording: booleans.has("--recording") };
  }
}

function parseFlags(step: StepName, args: readonly string[]) {
  const spec = STEP_FLAGS[step];
  const values = new Map<string, string>();
  const booleans = new Set<string>();
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (spec.booleans.includes(arg)) {
      booleans.add(arg);
    } else if (spec.values.includes(arg)) {
      const value = args[i + 1];
      if (value === undefined || value.startsWith("--")) {
        throw new UsageError(`${arg} needs a value`);
      }
      if (values.has(arg)) throw new UsageError(`${arg} is given twice`);
      values.set(arg, value);
      i++;
    } else {
      throw new UsageError(
        arg.startsWith("-") ? `${step} has no flag ${arg}` : `unexpected argument "${arg}"`,
      );
    }
  }
  return { values, booleans };
}

function required(values: Map<string, string>, flag: string): string {
  const value = values.get(flag);
  if (value === undefined) throw new UsageError(`${flag} is required`);
  return value;
}

/** A base58 address of 32 bytes, returned in its canonical form. */
export function parsePubkey(text: string, what: string): string {
  try {
    return new PublicKey(text).toBase58();
  } catch {
    throw new UsageError(`${what}: "${text}" is not a base58 public key`);
  }
}

/** `<pk>,<label>=<pk>,…`: no empty item, no duplicate key. */
export function parseSignerList(text: string, flag: string): SignerEntry[] {
  const entries = text.split(",").map((item): SignerEntry => {
    const trimmed = item.trim();
    if (trimmed === "") throw new UsageError(`${flag}: empty list item`);
    const eq = trimmed.indexOf("=");
    if (eq === -1) return { pubkey: parsePubkey(trimmed, flag) };
    const label = trimmed.slice(0, eq).trim();
    if (!/^[A-Za-z0-9 _-]{1,32}$/.test(label)) {
      throw new UsageError(
        `${flag}: label "${label}" must be 1-32 letters, digits, spaces, - or _`,
      );
    }
    return { pubkey: parsePubkey(trimmed.slice(eq + 1).trim(), flag), label };
  });
  const seen = new Set<string>();
  for (const { pubkey } of entries) {
    if (seen.has(pubkey)) throw new UsageError(`${flag}: ${pubkey} is listed twice`);
    seen.add(pubkey);
  }
  return entries;
}

/** A positive decimal token amount with at most `decimals` places, in base units. */
export function parseTokenAmount(text: string, flag: string, decimals: number): bigint {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(text);
  if (match === null || (match[2] ?? "").length > decimals) {
    throw new UsageError(`${flag}: "${text}" is not an amount with at most ${decimals} decimals`);
  }
  const units = BigInt(match[1] + (match[2] ?? "").padEnd(decimals, "0"));
  if (units === BigInt(0)) throw new UsageError(`${flag} must be more than 0`);
  return units;
}

/** Base units as a decimal string with `decimals` places trimmed, e.g. `100000000` → `100`. */
export function formatTokenAmount(units: bigint, decimals: number): string {
  const text = units.toString().padStart(decimals + 1, "0");
  const whole = text.slice(0, -decimals);
  const fraction = text.slice(-decimals).replace(/0+$/, "");
  return fraction === "" ? whole : `${whole}.${fraction}`;
}

function assertSmokePoId(po: string): void {
  if (!new RegExp(`^${SMOKE_PO_NUMBER}-[0-9A-HJKMNP-TV-Z]{4}$`).test(po)) {
    throw new UsageError(
      `--po takes a smoke chain_po_id such as ${SMOKE_PO_NUMBER}-7KQ2, got "${po}"`,
    );
  }
}

/** A fresh smoke `chain_po_id`, `SMOKE-<4 Crockford base32>`. */
export function newSmokePoId(): string {
  return newChainPoId(SMOKE_PO_NUMBER);
}

// ---- refusals ----

/** Accepts only an https URL that names devnet and no other cluster; never echoes the URL. */
export function assertDevnetRpcUrl(raw: string): URL {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new SetupError("SOLANA_RPC_URL is not a URL");
  }
  const where = `${url.hostname}${url.pathname}`.toLowerCase();
  if (url.protocol !== "https:") {
    throw new SetupError(`SOLANA_RPC_URL must be https (host ${url.hostname})`);
  }
  if (/mainnet|testnet/.test(where) || !where.includes("devnet")) {
    throw new SetupError(`SOLANA_RPC_URL must be a devnet endpoint, not ${url.hostname}`);
  }
  return url;
}

/** The second line of defence: the endpoint's genesis hash is devnet's. */
export function assertDevnetGenesis(genesisHash: string): void {
  if (genesisHash !== DEVNET_GENESIS_HASH) {
    throw new SetupError(`the RPC endpoint is not devnet (genesis hash ${genesisHash})`);
  }
}

function isInside(root: string, candidate: string): boolean {
  const rel = path.relative(root, candidate);
  return rel === "" || (rel !== ".." && !rel.startsWith(`..${path.sep}`) && !path.isAbsolute(rel));
}

/** Refuses a key path inside the repository, also through `..` or a symlink. */
export function assertKeyPathOutsideRepo(file: string, repoRoot: string): void {
  const root = fs.existsSync(repoRoot) ? fs.realpathSync.native(repoRoot) : path.resolve(repoRoot);
  const resolved = path.resolve(file);
  const candidates = [resolved];
  if (fs.existsSync(resolved)) candidates.push(fs.realpathSync.native(resolved));
  else if (fs.existsSync(path.dirname(resolved))) {
    candidates.push(
      path.join(fs.realpathSync.native(path.dirname(resolved)), path.basename(resolved)),
    );
  }
  if (candidates.some((candidate) => isInside(root, candidate))) {
    throw new SetupError(`${file} is inside the repository; key files live in $BIDO_KEYS_DIR`);
  }
}

/** `$BIDO_KEYS_DIR/<name>.json`, refused when the directory is unset or inside the repository. */
export function keyFilePath(keysDir: string | undefined, name: KeyName, repoRoot: string): string {
  if (keysDir === undefined || keysDir === "") {
    throw new SetupError("BIDO_KEYS_DIR is not set; key files live outside the repository");
  }
  const file = path.join(keysDir, `${name}.json`);
  assertKeyPathOutsideRepo(file, repoRoot);
  return file;
}

/** A solana-keygen file (64-number JSON array); the message never quotes the file. */
export function parseKeypairFile(text: string, label: string): Keypair {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new SetupError(`${label} is not a JSON keypair file`);
  }
  const ok =
    Array.isArray(parsed) &&
    parsed.length === 64 &&
    parsed.every((n) => typeof n === "number" && Number.isInteger(n) && n >= 0 && n <= 255);
  if (!ok) throw new SetupError(`${label} is not a 64-number JSON keypair file`);
  try {
    return Keypair.fromSecretKey(Uint8Array.from(parsed as number[]));
  } catch {
    throw new SetupError(`${label} does not hold a consistent ed25519 keypair`);
  }
}

// ---- deployments/devnet.json ----

export interface Measurements {
  cu: { createPo: number; fund: number; approval: number; release: number; refund: number };
  txBytes: { create_po: number; approve_ppap: number };
}

export interface CircleProof {
  chainPoId: string;
  escrow: string;
  signatures: string[];
}

/**
 * Public key → label. An index signature rather than `Record<string, string>`: instantiating that
 * `Record` this early in tsc's file order flips the inference of `src/proxy.test.ts`'s
 * `new Headers(names.map(…))` to `string[][]` (a type-ID ordering quirk).
 */
export type SignerLabels = { [pubkey: string]: string };

/** `deployments/devnet.json`: public data only (spec 01 §3.12). */
export interface Deployment {
  cluster: "devnet";
  programId: string;
  upgradeAuthority: string;
  configPda: string;
  eventAuthority: string;
  operator: string;
  relayer: string;
  tusdMint: string;
  tusdTreasuryOwner: string;
  /** Base units, as a decimal string. */
  tusdSupply: string;
  circleDevnetUsdcMint: string;
  sasProgramId: string;
  sasIssuer: string;
  sasCredential: string;
  sasSchema: string;
  /** Public key → label of every credential signer `sas` or `sas:signers` set. */
  sasSignerLabels: SignerLabels;
  /** The smoke attesters, removed from the credential at the freeze. */
  smokeSigners: string[];
  payoutOwner: string;
  payoutOwnerLabel: string;
  sasSoSha256: string;
  sasSoDumpSlot: number;
  build: { commit: string; executableHash: string; solanaVerifyVersion: string; frozenAt: string };
  measurements: Measurements;
  circleProof: CircleProof | null;
  /** Only once `alt` creates the lookup table (spec 01 §3.9 size trigger). */
  alt?: string;
}

const ADDRESS_FIELDS = [
  "programId",
  "upgradeAuthority",
  "configPda",
  "eventAuthority",
  "operator",
  "relayer",
  "tusdMint",
  "tusdTreasuryOwner",
  "circleDevnetUsdcMint",
  "sasProgramId",
  "sasIssuer",
  "sasCredential",
  "sasSchema",
  "payoutOwner",
] as const;
type AddressField = (typeof ADDRESS_FIELDS)[number];

const TOP_LEVEL_KEYS = [
  "cluster",
  ...ADDRESS_FIELDS.slice(0, 8),
  "tusdSupply",
  "circleDevnetUsdcMint",
  "sasProgramId",
  "sasIssuer",
  "sasCredential",
  "sasSchema",
  "sasSignerLabels",
  "smokeSigners",
  "payoutOwner",
  "payoutOwnerLabel",
  "sasSoSha256",
  "sasSoDumpSlot",
  "build",
  "measurements",
  "circleProof",
  "alt",
];

/** The §3.12 template: every field empty except the constants. */
export function emptyDeployment(): Deployment {
  return {
    cluster: "devnet",
    programId: "",
    upgradeAuthority: "",
    configPda: "",
    eventAuthority: "",
    operator: "",
    relayer: "",
    tusdMint: "",
    tusdTreasuryOwner: "",
    tusdSupply: TUSD_SUPPLY.toString(),
    circleDevnetUsdcMint: CIRCLE_DEVNET_USDC_MINT,
    sasProgramId: SAS_PROGRAM_ID,
    sasIssuer: "",
    sasCredential: "",
    sasSchema: "",
    sasSignerLabels: {},
    smokeSigners: [],
    payoutOwner: "",
    payoutOwnerLabel: PAYOUT_OWNER_LABEL,
    sasSoSha256: "",
    sasSoDumpSlot: 0,
    build: { commit: "", executableHash: "", solanaVerifyVersion: "", frozenAt: "" },
    measurements: {
      cu: { createPo: 0, fund: 0, approval: 0, release: 0, refund: 0 },
      txBytes: { create_po: 0, approve_ppap: 0 },
    },
    circleProof: null,
  };
}

type Json = Record<string, unknown>;

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isAddress(value: string): boolean {
  try {
    return new PublicKey(value).toBase58() === value;
  } catch {
    return false;
  }
}

/** Parses and checks `deployments/devnet.json`; refuses any cluster but devnet. */
export function parseDeployment(text: string): Deployment {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    throw new SetupError("deployments/devnet.json is not JSON");
  }
  const problems: string[] = [];
  if (!isObject(raw)) throw new SetupError("deployments/devnet.json is not a JSON object");
  for (const key of Object.keys(raw)) {
    if (!TOP_LEVEL_KEYS.includes(key)) problems.push(`unknown field "${key}"`);
  }
  if (raw.cluster !== "devnet") {
    throw new SetupError(
      `deployments/devnet.json cluster must be "devnet", got ${JSON.stringify(raw.cluster)}`,
    );
  }
  const str = (key: string): string => {
    const value = raw[key];
    if (typeof value !== "string") {
      problems.push(`${key} must be a string`);
      return "";
    }
    return value;
  };
  const addr = (key: AddressField): string => {
    const value = str(key);
    if (value !== "" && !isAddress(value)) problems.push(`${key} is not a base58 address`);
    return value;
  };
  const nonNegative = (where: string, value: unknown): number => {
    if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
      problems.push(`${where} must be a non-negative integer`);
      return 0;
    }
    return value;
  };
  const object = (where: string, value: unknown, keys: readonly string[]): Json => {
    if (!isObject(value)) {
      problems.push(`${where} must be an object`);
      return {};
    }
    const extra = Object.keys(value).filter((key) => !keys.includes(key));
    const missing = keys.filter((key) => !(key in value));
    if (extra.length > 0) problems.push(`${where} has unknown fields ${extra.join(", ")}`);
    if (missing.length > 0) problems.push(`${where} lacks ${missing.join(", ")}`);
    return value;
  };

  const tusdSupply = str("tusdSupply");
  if (!/^\d+$/.test(tusdSupply)) problems.push("tusdSupply must be a decimal string of base units");

  const labels: SignerLabels = {};
  if (!isObject(raw.sasSignerLabels)) problems.push("sasSignerLabels must be an object");
  else {
    for (const [key, value] of Object.entries(raw.sasSignerLabels)) {
      if (!isAddress(key)) problems.push(`sasSignerLabels key ${key} is not a base58 address`);
      if (typeof value !== "string") problems.push(`sasSignerLabels.${key} must be a string`);
      else labels[key] = value;
    }
  }
  const smokeSigners: string[] = [];
  if (!Array.isArray(raw.smokeSigners)) problems.push("smokeSigners must be an array");
  else {
    for (const value of raw.smokeSigners) {
      if (typeof value !== "string" || !isAddress(value)) {
        problems.push("smokeSigners holds a value that is not a base58 address");
      } else smokeSigners.push(value);
    }
  }

  const buildKeys = ["commit", "executableHash", "solanaVerifyVersion", "frozenAt"] as const;
  const rawBuild = object("build", raw.build, buildKeys);
  const build = { commit: "", executableHash: "", solanaVerifyVersion: "", frozenAt: "" };
  for (const key of buildKeys) {
    const value = rawBuild[key];
    if (typeof value === "string") build[key] = value;
    else if (key in rawBuild) problems.push(`build.${key} must be a string`);
  }

  const rawMeasurements = object("measurements", raw.measurements, ["cu", "txBytes"]);
  const cuKeys = ["createPo", "fund", "approval", "release", "refund"] as const;
  const bytesKeys = ["create_po", "approve_ppap"] as const;
  const rawCu = object("measurements.cu", rawMeasurements.cu, cuKeys);
  const rawBytes = object("measurements.txBytes", rawMeasurements.txBytes, bytesKeys);
  const measurements = emptyDeployment().measurements;
  for (const key of cuKeys)
    measurements.cu[key] = nonNegative(`measurements.cu.${key}`, rawCu[key] ?? 0);
  for (const key of bytesKeys) {
    measurements.txBytes[key] = nonNegative(`measurements.txBytes.${key}`, rawBytes[key] ?? 0);
  }

  let circleProof: CircleProof | null = null;
  if (raw.circleProof !== null) {
    const proof = object("circleProof", raw.circleProof, ["chainPoId", "escrow", "signatures"]);
    const { chainPoId, escrow, signatures } = proof;
    if (
      typeof chainPoId === "string" &&
      typeof escrow === "string" &&
      isAddress(escrow) &&
      Array.isArray(signatures) &&
      signatures.every((signature): signature is string => typeof signature === "string")
    ) {
      circleProof = { chainPoId, escrow, signatures };
    } else {
      problems.push("circleProof must be null or { chainPoId, escrow, signatures: string[] }");
    }
  }

  const deployment: Deployment = {
    cluster: "devnet",
    programId: addr("programId"),
    upgradeAuthority: addr("upgradeAuthority"),
    configPda: addr("configPda"),
    eventAuthority: addr("eventAuthority"),
    operator: addr("operator"),
    relayer: addr("relayer"),
    tusdMint: addr("tusdMint"),
    tusdTreasuryOwner: addr("tusdTreasuryOwner"),
    tusdSupply,
    circleDevnetUsdcMint: addr("circleDevnetUsdcMint"),
    sasProgramId: addr("sasProgramId"),
    sasIssuer: addr("sasIssuer"),
    sasCredential: addr("sasCredential"),
    sasSchema: addr("sasSchema"),
    sasSignerLabels: labels,
    smokeSigners,
    payoutOwner: addr("payoutOwner"),
    payoutOwnerLabel: str("payoutOwnerLabel"),
    sasSoSha256: str("sasSoSha256"),
    sasSoDumpSlot: nonNegative("sasSoDumpSlot", raw.sasSoDumpSlot),
    build,
    measurements,
    circleProof,
  };
  if ("alt" in raw) {
    const alt = raw.alt;
    if (typeof alt !== "string" || !isAddress(alt)) problems.push("alt must be a base58 address");
    else deployment.alt = alt;
  }
  if (deployment.sasSoSha256 !== "" && !/^[0-9a-f]{64}$/.test(deployment.sasSoSha256)) {
    problems.push("sasSoSha256 must be 64 lowercase hex digits");
  }
  if (problems.length > 0) {
    throw new SetupError(`deployments/devnet.json: ${problems.join("; ")}`);
  }
  return deployment;
}

/** The file's text: the §3.12 field order, two-space JSON, a final newline. */
export function serializeDeployment(d: Deployment): string {
  const ordered = {
    cluster: d.cluster,
    programId: d.programId,
    upgradeAuthority: d.upgradeAuthority,
    configPda: d.configPda,
    eventAuthority: d.eventAuthority,
    operator: d.operator,
    relayer: d.relayer,
    tusdMint: d.tusdMint,
    tusdTreasuryOwner: d.tusdTreasuryOwner,
    tusdSupply: d.tusdSupply,
    circleDevnetUsdcMint: d.circleDevnetUsdcMint,
    sasProgramId: d.sasProgramId,
    sasIssuer: d.sasIssuer,
    sasCredential: d.sasCredential,
    sasSchema: d.sasSchema,
    sasSignerLabels: Object.fromEntries(
      Object.entries(d.sasSignerLabels).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
    ),
    smokeSigners: d.smokeSigners,
    payoutOwner: d.payoutOwner,
    payoutOwnerLabel: d.payoutOwnerLabel,
    sasSoSha256: d.sasSoSha256,
    sasSoDumpSlot: d.sasSoDumpSlot,
    build: {
      commit: d.build.commit,
      executableHash: d.build.executableHash,
      solanaVerifyVersion: d.build.solanaVerifyVersion,
      frozenAt: d.build.frozenAt,
    },
    measurements: {
      cu: {
        createPo: d.measurements.cu.createPo,
        fund: d.measurements.cu.fund,
        approval: d.measurements.cu.approval,
        release: d.measurements.cu.release,
        refund: d.measurements.cu.refund,
      },
      txBytes: {
        create_po: d.measurements.txBytes.create_po,
        approve_ppap: d.measurements.txBytes.approve_ppap,
      },
    },
    circleProof:
      d.circleProof === null
        ? null
        : {
            chainPoId: d.circleProof.chainPoId,
            escrow: d.circleProof.escrow,
            signatures: d.circleProof.signatures,
          },
    ...(d.alt === undefined ? {} : { alt: d.alt }),
  };
  return `${JSON.stringify(ordered, null, 2)}\n`;
}

/** The `NEXT_PUBLIC_*` lines spec 10 sets from this file (00 §3.5); unset values as comments. */
export function envLines(d: Deployment): string[] {
  const entries: [string, string, string][] = [
    ["NEXT_PUBLIC_SOLANA_CLUSTER", d.cluster, ""],
    ["NEXT_PUBLIC_BIDO_ESCROW_PROGRAM_ID", d.programId, "config"],
    ["NEXT_PUBLIC_TUSD_MINT", d.tusdMint, "mint"],
    ["NEXT_PUBLIC_SAS_CREDENTIAL", d.sasCredential, "sas"],
    ["NEXT_PUBLIC_SAS_SCHEMA", d.sasSchema, "sas"],
  ];
  if (d.alt !== undefined) entries.push(["NEXT_PUBLIC_BIDO_ALT", d.alt, "alt"]);
  return entries.map(([name, value, step]) =>
    value === "" ? `# ${name} is not set yet (run \`${step}\`)` : `${name}=${value}`,
  );
}

// ---- chain reads ----

export interface AccountSnapshot {
  owner: string;
  executable: boolean;
  lamports: number;
  data: Uint8Array;
}

/** What the planners read; `chain-setup.ts` backs it with a devnet `Connection`. */
export interface ChainReader {
  getAccount(address: string): Promise<AccountSnapshot | null>;
  getGenesisHash(): Promise<string>;
}

export interface MintState {
  decimals: number;
  supply: bigint;
  mintAuthority: string | null;
  freezeAuthority: string | null;
}

export interface TokenAccountState {
  mint: string;
  owner: string;
  amount: bigint;
}

export interface CredentialState {
  authority: string;
  name: string;
  signers: string[];
}

export interface SchemaState {
  credential: string;
  name: string;
  description: string;
  layout: number[];
  fieldNames: string[];
  isPaused: boolean;
  version: number;
}

function accountInfo(snapshot: AccountSnapshot) {
  return {
    data: Buffer.from(snapshot.data),
    owner: new PublicKey(snapshot.owner),
    executable: snapshot.executable,
    lamports: snapshot.lamports,
  };
}

/** A classic SPL Token mint, or a `SetupError` naming `what`. */
export function decodeMint(
  addressText: string,
  snapshot: AccountSnapshot,
  what: string,
): MintState {
  try {
    const mint = unpackMint(new PublicKey(addressText), accountInfo(snapshot), TOKEN_PROGRAM_ID);
    return {
      decimals: mint.decimals,
      supply: mint.supply,
      mintAuthority: mint.mintAuthority?.toBase58() ?? null,
      freezeAuthority: mint.freezeAuthority?.toBase58() ?? null,
    };
  } catch {
    throw new SetupError(`${what} ${addressText} is not a classic SPL Token mint`);
  }
}

/** A classic SPL Token account, or a `SetupError` naming `what`. */
export function decodeTokenAccount(
  addressText: string,
  snapshot: AccountSnapshot,
  what: string,
): TokenAccountState {
  try {
    const account = unpackAccount(
      new PublicKey(addressText),
      accountInfo(snapshot),
      TOKEN_PROGRAM_ID,
    );
    return {
      mint: account.mint.toBase58(),
      owner: account.owner.toBase58(),
      amount: account.amount,
    };
  } catch {
    throw new SetupError(`${what} ${addressText} is not a classic SPL Token account`);
  }
}

export function decodeCredential(snapshot: AccountSnapshot): CredentialState {
  if (snapshot.owner !== SAS_PROGRAM_ID || snapshot.data[0] !== SAS_CREDENTIAL_DISC) {
    throw new SetupError("the SAS credential account is not a SAS credential");
  }
  const credential = getCredentialDecoder().decode(snapshot.data);
  return {
    authority: credential.authority,
    name: new TextDecoder().decode(new Uint8Array(credential.name)),
    signers: [...credential.authorizedSigners],
  };
}

export function decodeSchema(snapshot: AccountSnapshot): SchemaState {
  if (snapshot.owner !== SAS_PROGRAM_ID || snapshot.data[0] !== SAS_SCHEMA_DISC) {
    throw new SetupError("the SAS schema account is not a SAS schema");
  }
  const schema = getSchemaDecoder().decode(snapshot.data);
  return {
    credential: schema.credential,
    name: schema.name,
    description: schema.description,
    layout: schema.layout.map(Number),
    fieldNames: [...schema.fieldNames],
    isPaused: schema.isPaused,
    version: schema.version,
  };
}

/** `ProgramData` of an upgradeable program: `[program] → loader`. */
export function programDataAddress(programId: string): string {
  return PublicKey.findProgramAddressSync(
    [new PublicKey(programId).toBytes()],
    new PublicKey(UPGRADEABLE_LOADER_ID),
  )[0].toBase58();
}

/** The program account's `ProgramData` address; throws unless it is a deployed upgradeable program. */
export function decodeProgramAccount(snapshot: AccountSnapshot): string {
  const view = Buffer.from(snapshot.data);
  if (
    snapshot.owner !== UPGRADEABLE_LOADER_ID ||
    !snapshot.executable ||
    view.length < 36 ||
    view.readUInt32LE(0) !== LOADER_TAG_PROGRAM
  ) {
    throw new SetupError("the program account is not a deployed upgradeable program");
  }
  return new PublicKey(view.subarray(4, 36)).toBase58();
}

/** The upgrade authority (null once revoked) and the deployed ELF of a `ProgramData` account. */
export function decodeProgramData(snapshot: AccountSnapshot): {
  upgradeAuthority: string | null;
  elf: Uint8Array;
} {
  const view = Buffer.from(snapshot.data);
  if (
    snapshot.owner !== UPGRADEABLE_LOADER_ID ||
    view.length < PROGRAM_DATA_HEADER_LEN ||
    view.readUInt32LE(0) !== LOADER_TAG_PROGRAM_DATA
  ) {
    throw new SetupError("the ProgramData account is not upgradeable-loader program data");
  }
  const upgradeAuthority = view[12] === 1 ? new PublicKey(view.subarray(13, 45)).toBase58() : null;
  return { upgradeAuthority, elf: new Uint8Array(view.subarray(PROGRAM_DATA_HEADER_LEN)) };
}

/** `solana-verify get-program-hash`: sha256 of the deployed ELF without its zero padding. */
export function programHash(elf: Uint8Array): string {
  let end = elf.length;
  while (end > 0 && elf[end - 1] === 0) end--;
  return createHash("sha256").update(elf.subarray(0, end)).digest("hex");
}

// ---- planners ----

export type MintAction =
  | { kind: "create_mint" }
  | { kind: "mint_remaining"; amount: bigint }
  | { kind: "revoke_mint_authority" };

/**
 * What `mint` still has to do. No recorded mint: create it (one transaction: account, mint,
 * treasury ATA, full supply, authority revoked). A recorded mint: finish the supply and the
 * revocation if the treasury still holds the authority; any other difference is fatal.
 */
export function planMint({
  recordedMint,
  mint,
  treasury,
}: {
  recordedMint: string;
  mint: MintState | null;
  treasury: string;
}): MintAction[] {
  if (recordedMint === "") return [{ kind: "create_mint" }];
  if (mint === null) throw new SetupError(`the recorded tUSD mint ${recordedMint} does not exist`);
  if (mint.decimals !== TUSD_DECIMALS) {
    throw new SetupError(`tUSD has ${mint.decimals} decimals, not ${TUSD_DECIMALS}`);
  }
  if (mint.freezeAuthority !== null) {
    throw new SetupError(`tUSD has freeze authority ${mint.freezeAuthority}; it must have none`);
  }
  if (mint.supply > TUSD_SUPPLY) {
    throw new SetupError(`tUSD supply ${mint.supply} is above ${TUSD_SUPPLY} base units`);
  }
  if (mint.mintAuthority !== null && mint.mintAuthority !== treasury) {
    throw new SetupError(`tUSD mint authority is ${mint.mintAuthority}, not treasury.json`);
  }
  if (mint.supply < TUSD_SUPPLY) {
    if (mint.mintAuthority === null) {
      throw new SetupError(`tUSD supply is ${mint.supply} and the mint authority is revoked`);
    }
    return [
      { kind: "mint_remaining", amount: TUSD_SUPPLY - mint.supply },
      { kind: "revoke_mint_authority" },
    ];
  }
  return mint.mintAuthority === null ? [] : [{ kind: "revoke_mint_authority" }];
}

export type SasAction = { kind: "create_credential" } | { kind: "create_schema" };

/** The schema §3.12 wants, as `SchemaState`. */
export function wantedSchema(credential: string): SchemaState {
  return {
    credential,
    name: SAS_SCHEMA_NAME,
    description: SAS_SCHEMA_DESCRIPTION,
    layout: [...SAS_SCHEMA_LAYOUT],
    fieldNames: [...SAS_SCHEMA_FIELDS],
    isPaused: false,
    version: SAS_SCHEMA_VERSION,
  };
}

/** Field-by-field differences between an on-chain schema and the wanted one. */
export function schemaDifferences(actual: SchemaState, wanted: SchemaState): string[] {
  const keys = Object.keys(wanted) as (keyof SchemaState)[];
  return keys
    .filter((key) => JSON.stringify(actual[key]) !== JSON.stringify(wanted[key]))
    .map(
      (key) => `${key} is ${JSON.stringify(actual[key])}, wanted ${JSON.stringify(wanted[key])}`,
    );
}

/**
 * What `sas` still has to create. An existing credential must belong to the issuer; an existing
 * schema that differs stops the step with the fix (SAS schemas are immutable but for status,
 * description and version).
 */
export function planSas({
  issuer,
  credentialAddress,
  credential,
  schema,
}: {
  issuer: string;
  credentialAddress: string;
  credential: CredentialState | null;
  schema: SchemaState | null;
}): SasAction[] {
  const actions: SasAction[] = [];
  if (credential === null) actions.push({ kind: "create_credential" });
  else if (credential.authority !== issuer || credential.name !== SAS_CREDENTIAL_NAME) {
    throw new SetupError(
      `credential ${credentialAddress} has authority ${credential.authority} and name "${credential.name}"; wanted sas-issuer.json and "${SAS_CREDENTIAL_NAME}"`,
    );
  }
  if (schema === null) actions.push({ kind: "create_schema" });
  else {
    const differences = schemaDifferences(schema, wantedSchema(credentialAddress));
    if (differences.length > 0) {
      throw new SetupError(
        [
          `schema "${SAS_SCHEMA_NAME}" v${SAS_SCHEMA_VERSION} differs: ${differences.join("; ")}.`,
          "Fix: publish a matching schema with SAS ChangeSchemaVersion, or create one under a new name",
          "and point Config at it with update_config (then record it here).",
        ].join(" "),
      );
    }
  }
  return actions;
}

/** True when the credential's signers are not exactly `wanted` (as sets). */
export function signersNeedChange(current: readonly string[], wanted: readonly string[]): boolean {
  const have = new Set(current);
  return have.size !== new Set(wanted).size || wanted.some((pubkey) => !have.has(pubkey));
}

/** The labels `sas:signers` records: given labels, then known ones, then `signer`. */
export function signerLabels(entries: readonly SignerEntry[], known: SignerLabels): SignerLabels {
  return Object.fromEntries(
    entries.map(({ pubkey, label }) => [pubkey, label ?? known[pubkey] ?? "signer"]),
  );
}

export interface WantedConfig {
  admin: string;
  operator: string;
  allowedMints: [string, string];
  sasCredential: string;
  sasSchema: string;
}

export interface UpdateConfigFields {
  operator: string | null;
  allowedMints: [string, string] | null;
  sasCredential: string | null;
  sasSchema: string | null;
}

export type ConfigPlan =
  { kind: "init" } | { kind: "update"; args: UpdateConfigFields } | { kind: "none" };

/**
 * `init_config` when `Config` does not exist; otherwise `update_config` with only the differing
 * fields, also when only `admin` differs (an upgrade-authority rotation: the call syncs it).
 */
export function planConfig(wanted: WantedConfig, current: ConfigAccount | null): ConfigPlan {
  if (current === null) return { kind: "init" };
  const mintsDiffer = wanted.allowedMints.some((mint, i) => current.allowedMints[i] !== mint);
  const args: UpdateConfigFields = {
    operator: current.operator === wanted.operator ? null : wanted.operator,
    allowedMints: mintsDiffer ? wanted.allowedMints : null,
    sasCredential: current.sasCredential === wanted.sasCredential ? null : wanted.sasCredential,
    sasSchema: current.sasSchema === wanted.sasSchema ? null : wanted.sasSchema,
  };
  const changed = Object.values(args).some((value) => value !== null);
  return changed || current.admin !== wanted.admin ? { kind: "update", args } : { kind: "none" };
}

/** Whether `payout` must create the payout owner's tUSD account; a foreign account is fatal. */
export function planPayout({
  ataAddress,
  ata,
  owner,
  mint,
}: {
  ataAddress: string;
  ata: TokenAccountState | null;
  owner: string;
  mint: string;
}): boolean {
  if (ata === null) return true;
  if (ata.owner !== owner || ata.mint !== mint) {
    throw new SetupError(`${ataAddress} is not the tUSD account of ${owner}`);
  }
  return false;
}

/** `max(0, target − balance)`: what a top-up transfers (spec 11 `plan_top_up`). */
export function planTopUp(balance: bigint, target: bigint): bigint {
  return balance >= target ? BigInt(0) : target - balance;
}

export type SmokeAction = "create_po" | "fund" | "approve_gate0" | "approve_gate1";

/**
 * The smoke steps still due for one escrow, in order. `until: "gate0"` (`smoke --only gate0`) stops
 * after the gate-0 approval; `until: "fund"` (the failed-release escrow) after funding.
 */
export function planSmoke(state: PoEscrowAccount | null, until?: "gate0" | "fund"): SmokeAction[] {
  let actions: SmokeAction[];
  if (state === null) actions = ["create_po", "fund", "approve_gate0", "approve_gate1"];
  else if (state.status === "Created") actions = ["fund", "approve_gate0", "approve_gate1"];
  else if (state.status === "Funded") {
    const pending = (gate: number) =>
      state.milestones.some((m) => m.gate === gate && m.status === "Pending");
    actions = [
      ...(pending(0) ? (["approve_gate0"] as const) : []),
      ...(pending(1) ? (["approve_gate1"] as const) : []),
    ];
  } else if (state.status === "Completed") actions = [];
  else throw new SetupError(`smoke escrow ${state.chainPoId} is ${state.status}`);
  if (until === "fund") return actions.filter((a) => a === "create_po" || a === "fund");
  return until === "gate0" ? actions.filter((a) => a !== "approve_gate1") : actions;
}

/** Each measurement is the maximum ever seen (LiteSVM or devnet, spec 01 §3.9). */
export function mergeMeasurements(
  current: Measurements,
  seen: { cu?: Partial<Measurements["cu"]>; txBytes?: Partial<Measurements["txBytes"]> },
): Measurements {
  const cu = { ...current.cu };
  const txBytes = { ...current.txBytes };
  for (const key of Object.keys(cu) as (keyof Measurements["cu"])[]) {
    cu[key] = Math.max(cu[key], seen.cu?.[key] ?? 0);
  }
  for (const key of Object.keys(txBytes) as (keyof Measurements["txBytes"])[]) {
    txBytes[key] = Math.max(txBytes[key], seen.txBytes?.[key] ?? 0);
  }
  return { cu, txBytes };
}

export type AltPlan =
  | { kind: "not_needed" }
  | { kind: "unmeasured" }
  | { kind: "create"; addresses: string[] }
  | { kind: "extend"; addresses: string[] }
  | { kind: "none" };

/**
 * `alt` acts only when a measured transaction is over `MAX_TX_BYTES` (or a table already exists):
 * it creates the table, or extends it with the static addresses it lacks.
 */
export function planAlt({
  measurements,
  wanted,
  existing,
}: {
  measurements: Measurements;
  wanted: readonly string[];
  existing: string[] | null;
}): AltPlan {
  if (existing !== null) {
    const missing = wanted.filter((a) => !existing.includes(a));
    return missing.length > 0 ? { kind: "extend", addresses: missing } : { kind: "none" };
  }
  const sizes = Object.values(measurements.txBytes);
  if (sizes.some((size) => size > MAX_TX_BYTES)) return { kind: "create", addresses: [...wanted] };
  return sizes.every((size) => size > 0) ? { kind: "not_needed" } : { kind: "unmeasured" };
}

/**
 * The lookup table's addresses: the static accounts of the `create_po` and approval transactions
 * that are not invoked programs (a v0 message keeps invoked program ids in its static keys).
 */
export function altAddresses(d: Deployment): string[] {
  return [
    TOKEN_PROGRAM_ID.toBase58(),
    ASSOCIATED_TOKEN_PROGRAM_ID.toBase58(),
    SystemProgram.programId.toBase58(),
    d.configPda,
    d.eventAuthority,
    d.sasCredential,
    d.sasSchema,
    d.tusdMint,
  ].filter((a) => a !== "");
}

export function decodeLookupTable(addressText: string, snapshot: AccountSnapshot): string[] {
  try {
    return AddressLookupTableAccount.deserialize(snapshot.data).addresses.map((a) => a.toBase58());
  } catch {
    throw new SetupError(`${addressText} is not an address lookup table`);
  }
}

// ---- verify ----

const RECORDED_BY: Partial<Record<AddressField, string>> = {
  programId: "the program id from IMPLEMENTATION_PLAN.md",
  upgradeAuthority: "config",
  configPda: "config",
  eventAuthority: "config",
  operator: "keys:check",
  relayer: "keys:check",
  tusdMint: "mint",
  tusdTreasuryOwner: "mint",
  sasIssuer: "sas",
  sasCredential: "sas",
  sasSchema: "sas",
  payoutOwner: "payout",
};

/** Credential signers `verify --recording` requires: the labelled signers that are not smoke keys. */
export function recordingSigners(d: Deployment): string[] {
  return Object.keys(d.sasSignerLabels).filter((pubkey) => !d.smokeSigners.includes(pubkey));
}

/**
 * Re-reads everything in `deployments/devnet.json` from chain (ESC-AC-17) and returns each problem;
 * empty means green. `recording` adds ESC-AC-21: signers exactly the two non-smoke ones, and the
 * deployed program hash equal to `build.executableHash`.
 */
export async function verifyDeployment(
  d: Deployment,
  chain: ChainReader,
  { recording }: { recording: boolean },
): Promise<string[]> {
  const problems: string[] = [];
  const genesis = await chain.getGenesisHash();
  if (genesis !== DEVNET_GENESIS_HASH) problems.push(`the RPC endpoint is not devnet (${genesis})`);

  for (const [field, step] of Object.entries(RECORDED_BY) as [AddressField, string][]) {
    if (d[field] === "") problems.push(`${field} is not recorded (run \`${step}\`)`);
  }
  if (d.sasProgramId !== SAS_PROGRAM_ID) problems.push(`sasProgramId is not ${SAS_PROGRAM_ID}`);
  if (d.circleDevnetUsdcMint !== CIRCLE_DEVNET_USDC_MINT) {
    problems.push(`circleDevnetUsdcMint is not ${CIRCLE_DEVNET_USDC_MINT}`);
  }
  if (d.payoutOwnerLabel !== PAYOUT_OWNER_LABEL) {
    problems.push(`payoutOwnerLabel is not "${PAYOUT_OWNER_LABEL}"`);
  }
  if (d.tusdSupply !== TUSD_SUPPLY.toString()) {
    problems.push(`tusdSupply is ${d.tusdSupply}, not ${TUSD_SUPPLY}`);
  }
  if (d.programId === "") return problems;

  const programId = new PublicKey(d.programId);
  const derived = {
    configPda: configPda(programId).toBase58(),
    eventAuthority: eventAuthorityPda(programId).toBase58(),
  };
  for (const key of ["configPda", "eventAuthority"] as const) {
    if (d[key] !== "" && d[key] !== derived[key]) {
      problems.push(`${key} is ${d[key]}, but the program derives ${derived[key]}`);
    }
  }

  let deployedHash: string | null = null;
  const program = await chain.getAccount(d.programId);
  if (program === null) problems.push(`program ${d.programId} is not deployed`);
  else {
    try {
      const dataAddress = decodeProgramAccount(program);
      const programData = await chain.getAccount(dataAddress);
      if (programData === null) throw new SetupError(`ProgramData ${dataAddress} does not exist`);
      const { upgradeAuthority, elf } = decodeProgramData(programData);
      deployedHash = programHash(elf);
      if (d.upgradeAuthority !== "" && upgradeAuthority !== d.upgradeAuthority) {
        problems.push(`the upgrade authority is ${upgradeAuthority}, not ${d.upgradeAuthority}`);
      }
    } catch (error) {
      problems.push(messageOf(error));
    }
  }

  const config = await chain.getAccount(derived.configPda);
  if (config === null) problems.push(`Config ${derived.configPda} does not exist (run \`config\`)`);
  else {
    try {
      const state = decodeConfig(config.data);
      const wanted: [keyof ConfigAccount, string][] = [
        ["admin", d.upgradeAuthority],
        ["operator", d.operator],
        ["sasCredential", d.sasCredential],
        ["sasSchema", d.sasSchema],
      ];
      for (const [key, value] of wanted) {
        if (state[key] !== value)
          problems.push(`Config.${key} is ${String(state[key])}, not ${value}`);
      }
      const mints = [d.tusdMint, d.circleDevnetUsdcMint];
      if (state.allowedMints.some((mint, i) => mint !== mints[i])) {
        problems.push(
          `Config.allowedMints is [${state.allowedMints.join(", ")}], not [${mints.join(", ")}]`,
        );
      }
    } catch (error) {
      problems.push(`Config does not decode: ${messageOf(error)}`);
    }
  }

  if (d.tusdMint !== "") {
    const mintAccount = await chain.getAccount(d.tusdMint);
    if (mintAccount === null) problems.push(`tUSD mint ${d.tusdMint} does not exist`);
    else {
      try {
        const mint = decodeMint(d.tusdMint, mintAccount, "tUSD mint");
        if (mint.decimals !== TUSD_DECIMALS) problems.push(`tUSD has ${mint.decimals} decimals`);
        if (mint.supply !== TUSD_SUPPLY)
          problems.push(`tUSD supply is ${mint.supply}, not ${TUSD_SUPPLY}`);
        if (mint.mintAuthority !== null)
          problems.push(`tUSD mint authority is ${mint.mintAuthority}`);
        if (mint.freezeAuthority !== null) {
          problems.push(`tUSD freeze authority is ${mint.freezeAuthority}`);
        }
      } catch (error) {
        problems.push(messageOf(error));
      }
    }
    if (d.tusdTreasuryOwner !== "") {
      problems.push(
        ...(await tokenAccountProblems(chain, d.tusdTreasuryOwner, d.tusdMint, "treasury")),
      );
    }
    if (d.payoutOwner !== "") {
      problems.push(...(await tokenAccountProblems(chain, d.payoutOwner, d.tusdMint, "payout")));
    }
  }

  if (d.sasIssuer !== "") {
    const expected = credentialPda(new PublicKey(d.sasIssuer), SAS_CREDENTIAL_NAME).toBase58();
    if (d.sasCredential !== expected) {
      problems.push(`sasCredential is ${d.sasCredential}, but the issuer derives ${expected}`);
    }
  }
  if (d.sasCredential !== "") {
    const expectedSchema = schemaPda(
      new PublicKey(d.sasCredential),
      SAS_SCHEMA_NAME,
      SAS_SCHEMA_VERSION,
    ).toBase58();
    if (d.sasSchema !== expectedSchema) {
      problems.push(`sasSchema is ${d.sasSchema}, but the credential derives ${expectedSchema}`);
    }
    const credentialAccount = await chain.getAccount(d.sasCredential);
    if (credentialAccount === null) problems.push(`credential ${d.sasCredential} does not exist`);
    else {
      try {
        const credential = decodeCredential(credentialAccount);
        if (credential.authority !== d.sasIssuer) {
          problems.push(`credential authority is ${credential.authority}, not ${d.sasIssuer}`);
        }
        if (credential.name !== SAS_CREDENTIAL_NAME) {
          problems.push(`credential name is "${credential.name}"`);
        }
        const recorded = Object.keys(d.sasSignerLabels);
        if (recorded.length > 0 && signersNeedChange(credential.signers, recorded)) {
          problems.push(
            `credential signers are [${credential.signers.join(", ")}], not the recorded [${recorded.join(", ")}]`,
          );
        }
        if (recording) {
          const wanted = recordingSigners(d);
          if (wanted.length !== 2 || signersNeedChange(credential.signers, wanted)) {
            problems.push(
              `--recording: credential signers must be exactly the buyer approver and the SQE [${wanted.join(", ")}], found [${credential.signers.join(", ")}]`,
            );
          }
        }
      } catch (error) {
        problems.push(messageOf(error));
      }
    }
    const schemaAccount = await chain.getAccount(d.sasSchema);
    if (schemaAccount === null) problems.push(`schema ${d.sasSchema} does not exist`);
    else {
      try {
        const differences = schemaDifferences(
          decodeSchema(schemaAccount),
          wantedSchema(d.sasCredential),
        );
        problems.push(...differences.map((difference) => `schema ${difference}`));
      } catch (error) {
        problems.push(messageOf(error));
      }
    }
  }

  if (d.alt !== undefined) {
    const table = await chain.getAccount(d.alt);
    if (table === null) problems.push(`lookup table ${d.alt} does not exist`);
    else {
      try {
        const addresses = decodeLookupTable(d.alt, table);
        const missing = altAddresses(d).filter((a) => !addresses.includes(a));
        if (missing.length > 0) problems.push(`lookup table lacks ${missing.join(", ")}`);
      } catch (error) {
        problems.push(messageOf(error));
      }
    }
  }

  if (recording) {
    if (d.build.executableHash === "")
      problems.push("--recording: build.executableHash is not recorded");
    else if (deployedHash !== null && deployedHash !== d.build.executableHash) {
      problems.push(
        `--recording: the program hash is ${deployedHash}, not build.executableHash ${d.build.executableHash}`,
      );
    }
  }
  return problems;
}

async function tokenAccountProblems(
  chain: ChainReader,
  owner: string,
  mint: string,
  what: string,
): Promise<string[]> {
  const ata = payoutAta(new PublicKey(owner), new PublicKey(mint)).toBase58();
  const account = await chain.getAccount(ata);
  if (account === null) return [`the ${what} tUSD account ${ata} does not exist`];
  try {
    const state = decodeTokenAccount(ata, account, `${what} tUSD account`);
    return state.owner === owner && state.mint === mint
      ? []
      : [`the ${what} tUSD account ${ata} is not owned by ${owner} for ${mint}`];
  } catch (error) {
    return [messageOf(error)];
  }
}

export function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

// ---- instruction builders ----

/** `[SetComputeUnitLimit, SetComputeUnitPrice]`, the prefix of every Bido transaction (§3.9). */
export function computeBudgetIxs(units: number): TransactionInstruction[] {
  return [
    ComputeBudgetProgram.setComputeUnitLimit({ units }),
    ComputeBudgetProgram.setComputeUnitPrice({ microLamports: SETUP_CU_PRICE_MICRO_LAMPORTS }),
  ];
}

/** The `CU_LIMITS` entry of a transaction; `release` is per release instruction. */
export function cuLimitFor(kind: keyof typeof CU_LIMITS, releases = 1): number {
  return kind === "release" ? CU_LIMITS.release * releases : CU_LIMITS[kind];
}

/** `init_config(operator, allowed_mints)`, signed and paid by the upgrade authority. */
export function initConfigIx({
  programId,
  admin,
  operator,
  allowedMints,
  credential,
  schema,
}: {
  programId: PublicKey;
  admin: PublicKey;
  operator: PublicKey;
  allowedMints: [PublicKey, PublicKey];
  credential: PublicKey;
  schema: PublicKey;
}): TransactionInstruction {
  return new TransactionInstruction({
    programId,
    keys: [
      { pubkey: admin, isSigner: true, isWritable: true },
      { pubkey: configPda(programId), isSigner: false, isWritable: true },
      { pubkey: programId, isSigner: false, isWritable: false },
      {
        pubkey: new PublicKey(programDataAddress(programId.toBase58())),
        isSigner: false,
        isWritable: false,
      },
      { pubkey: credential, isSigner: false, isWritable: false },
      { pubkey: schema, isSigner: false, isWritable: false },
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
    ],
    data: escrowCoder.instruction.encode("init_config", {
      args: { operator, allowed_mints: allowedMints },
    }),
  });
}

/** `update_config` with `null` for every unchanged field; `credential`/`schema` are the resulting pair. */
export function updateConfigIx({
  programId,
  admin,
  args,
  credential,
  schema,
}: {
  programId: PublicKey;
  admin: PublicKey;
  args: UpdateConfigFields;
  credential: PublicKey;
  schema: PublicKey;
}): TransactionInstruction {
  const key = (value: string | null) => (value === null ? null : new PublicKey(value));
  return new TransactionInstruction({
    programId,
    keys: [
      { pubkey: admin, isSigner: true, isWritable: false },
      { pubkey: configPda(programId), isSigner: false, isWritable: true },
      { pubkey: programId, isSigner: false, isWritable: false },
      {
        pubkey: new PublicKey(programDataAddress(programId.toBase58())),
        isSigner: false,
        isWritable: false,
      },
      { pubkey: credential, isSigner: false, isWritable: false },
      { pubkey: schema, isSigner: false, isWritable: false },
    ],
    data: escrowCoder.instruction.encode("update_config", {
      args: {
        operator: key(args.operator),
        allowed_mints:
          args.allowedMints === null ? null : args.allowedMints.map((m) => new PublicKey(m)),
        sas_credential: key(args.sasCredential),
        sas_schema: key(args.sasSchema),
      },
    }),
  });
}

interface KitInstruction {
  programAddress: string;
  accounts?: readonly { address: string; role: AccountRole }[];
  data?: ArrayLike<number>;
}

/** A `@solana/kit` instruction as a web3.js one. */
export function fromKitInstruction(ix: KitInstruction): TransactionInstruction {
  return new TransactionInstruction({
    programId: new PublicKey(ix.programAddress),
    keys: (ix.accounts ?? []).map((meta) => ({
      pubkey: new PublicKey(meta.address),
      isSigner:
        meta.role === AccountRole.READONLY_SIGNER || meta.role === AccountRole.WRITABLE_SIGNER,
      isWritable: meta.role === AccountRole.WRITABLE || meta.role === AccountRole.WRITABLE_SIGNER,
    })),
    data: Buffer.from(Uint8Array.from(ix.data ?? [])),
  });
}

const signer = (key: PublicKey) => createNoopSigner(address(key.toBase58()));

/** SAS `CreateCredential` "bido-approvals", issuer as payer and authority. */
export function createCredentialIx(
  issuer: PublicKey,
  signers: readonly string[],
): TransactionInstruction {
  return fromKitInstruction(
    getCreateCredentialInstruction({
      payer: signer(issuer),
      credential: address(credentialPda(issuer, SAS_CREDENTIAL_NAME).toBase58()),
      authority: signer(issuer),
      name: SAS_CREDENTIAL_NAME,
      signers: signers.map((pubkey) => address(pubkey)),
    }),
  );
}

/** SAS `CreateSchema` "bido-gate-approval" v1 with the §3.12 description, layout and fields. */
export function createSchemaIx(issuer: PublicKey, credential: PublicKey): TransactionInstruction {
  return fromKitInstruction(
    getCreateSchemaInstruction({
      payer: signer(issuer),
      authority: signer(issuer),
      credential: address(credential.toBase58()),
      schema: address(schemaPda(credential, SAS_SCHEMA_NAME, SAS_SCHEMA_VERSION).toBase58()),
      name: SAS_SCHEMA_NAME,
      description: SAS_SCHEMA_DESCRIPTION,
      layout: [...SAS_SCHEMA_LAYOUT],
      fieldNames: [...SAS_SCHEMA_FIELDS],
    }),
  );
}

/** SAS `ChangeAuthorizedSigners` (tag 3) to exactly `signers`. */
export function changeAuthorizedSignersIx(
  issuer: PublicKey,
  credential: PublicKey,
  signers: readonly string[],
): TransactionInstruction {
  return fromKitInstruction(
    getChangeAuthorizedSignersInstruction({
      payer: signer(issuer),
      authority: signer(issuer),
      credential: address(credential.toBase58()),
      signers: signers.map((pubkey) => address(pubkey)),
    }),
  );
}

/**
 * The `mint` transaction: a fresh mint account, 6 decimals, mint authority the treasury and no
 * freeze authority, the treasury's ATA, the full supply, then the mint authority revoked.
 */
export function createMintIxs({
  treasury,
  mint,
  rentLamports,
}: {
  treasury: PublicKey;
  mint: PublicKey;
  rentLamports: number;
}): TransactionInstruction[] {
  const ata = getAssociatedTokenAddressSync(mint, treasury);
  return [
    SystemProgram.createAccount({
      fromPubkey: treasury,
      newAccountPubkey: mint,
      lamports: rentLamports,
      space: MINT_SIZE,
      programId: TOKEN_PROGRAM_ID,
    }),
    createInitializeMint2Instruction(mint, TUSD_DECIMALS, treasury, null),
    createAssociatedTokenAccountIdempotentInstruction(treasury, ata, treasury, mint),
    createMintToCheckedInstruction(mint, ata, treasury, TUSD_SUPPLY, TUSD_DECIMALS),
    createSetAuthorityInstruction(mint, treasury, AuthorityType.MintTokens, null),
  ];
}

/** The rest of an interrupted `mint`: mint `amount` more to the treasury ATA, then revoke. */
export function finishMintIxs(treasury: PublicKey, mint: PublicKey, actions: MintAction[]) {
  const ata = getAssociatedTokenAddressSync(mint, treasury);
  return actions.flatMap((action): TransactionInstruction[] => {
    if (action.kind === "mint_remaining") {
      return [
        createAssociatedTokenAccountIdempotentInstruction(treasury, ata, treasury, mint),
        createMintToCheckedInstruction(mint, ata, treasury, action.amount, TUSD_DECIMALS),
      ];
    }
    if (action.kind === "revoke_mint_authority") {
      return [createSetAuthorityInstruction(mint, treasury, AuthorityType.MintTokens, null)];
    }
    throw new SetupError("create_mint is built by createMintIxs");
  });
}

/** Treasury → `to` transfer of `amount`, creating `to`'s ATA if needed (the treasury pays). */
export function topUpIxs({
  treasury,
  mint,
  to,
  amount,
  decimals,
}: {
  treasury: PublicKey;
  mint: PublicKey;
  to: PublicKey;
  amount: bigint;
  decimals: number;
}): TransactionInstruction[] {
  const source = getAssociatedTokenAddressSync(mint, treasury);
  const destination = getAssociatedTokenAddressSync(mint, to, true);
  return [
    createAssociatedTokenAccountIdempotentInstruction(treasury, destination, to, mint),
    createTransferCheckedInstruction(source, mint, destination, treasury, amount, decimals),
  ];
}

/** The payout owner's ATA for `mint`, created idempotently by `payer`. */
export function createPayoutAtaIx(payer: PublicKey, owner: PublicKey, mint: PublicKey) {
  return createAssociatedTokenAccountIdempotentInstruction(
    payer,
    payoutAta(owner, mint),
    owner,
    mint,
  );
}

/** The lookup-table transactions: create (if `table` is null) and extend with `addresses`. */
export function lookupTableIxs({
  authority,
  recentSlot,
  table,
  addresses,
}: {
  authority: PublicKey;
  recentSlot: number;
  table: PublicKey | null;
  addresses: readonly string[];
}): { table: PublicKey; instructions: TransactionInstruction[] } {
  const instructions: TransactionInstruction[] = [];
  let lookupTable = table;
  if (lookupTable === null) {
    const [create, created] = AddressLookupTableProgram.createLookupTable({
      authority,
      payer: authority,
      recentSlot,
    });
    instructions.push(create);
    lookupTable = created;
  }
  instructions.push(
    AddressLookupTableProgram.extendLookupTable({
      payer: authority,
      authority,
      lookupTable,
      addresses: addresses.map((a) => new PublicKey(a)),
    }),
  );
  return { table: lookupTable, instructions };
}

/** A smoke file hash: sha256 of a labelled string, as lowercase hex. */
export function smokeHashHex(label: string): string {
  return Buffer.from(sha256(new TextEncoder().encode(label))).toString("hex");
}

/**
 * The `create_po` arguments of a smoke escrow: the demo milestones (`MILESTONE_POLICY`), gate 0
 * attested by `smoke-gate0`, gate 1 by `smoke-gate1`, hashes derived from the id, refund in 90 days;
 * and the `payout_ref` of its Memo.
 */
export function smokePoArgs({
  chainPoId,
  total,
  gateAttesters,
  nowSeconds,
}: {
  chainPoId: string;
  total: bigint;
  gateAttesters: [PublicKey, PublicKey];
  nowSeconds: number;
}): { args: CreatePoArgs; payoutRefHex: string } {
  return {
    args: {
      chainPoId,
      poHash: smokeHashHex(`bido-smoke:po:${chainPoId}`),
      cadHash: smokeHashHex(`bido-smoke:cad:${chainPoId}`),
      total,
      gateAttesters,
      milestones: MILESTONE_POLICY.map(({ name, bps, gate }) => ({ name, bps, gate })),
      refundAfter: BigInt(Math.floor(nowSeconds) + ESCROW_REFUND_AFTER_SECONDS),
    },
    payoutRefHex: smokeHashHex(`bido-smoke:payout-ref:${chainPoId}`),
  };
}

// ---- transaction results ----

/**
 * Compute units of each top-level invocation of `programId`, from a transaction's logs: the
 * `consumed` line at invoke depth 1 (self-CPIs such as `emit_cpi!` log at depth 2).
 */
export function topLevelUnits(logs: readonly string[], programId: string): number[] {
  const units: number[] = [];
  let depth = 0;
  for (const line of logs) {
    const invoke = /^Program (\S+) invoke \[(\d+)\]$/.exec(line);
    if (invoke !== null) {
      depth = Number(invoke[2]);
      continue;
    }
    const consumed = /^Program (\S+) consumed (\d+) of \d+ compute units$/.exec(line);
    if (consumed !== null && depth === 1 && consumed[1] === programId) {
      units.push(Number(consumed[2]));
      continue;
    }
    if (/^Program \S+ (success|failed: .*)$/.test(line)) depth = Math.max(0, depth - 1);
  }
  return units;
}

/** The custom program error code of a failed transaction's `err`, or null. */
export function customErrorCode(err: unknown): number | null {
  if (!isObject(err) || !Array.isArray(err.InstructionError)) return null;
  const detail: unknown = err.InstructionError[1];
  return isObject(detail) && typeof detail.Custom === "number" ? detail.Custom : null;
}

export function explorerTxUrl(signature: string): string {
  return `${EXPLORER_TX_URL}${signature}?cluster=devnet`;
}
