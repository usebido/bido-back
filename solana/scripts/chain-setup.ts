/**
 * One-time devnet setup and smoke runs (spec 01 §3.12):
 *
 *   pnpm --dir solana exec tsx scripts/chain-setup.ts <step> [flags]
 *
 * Steps: `keys:check`, `mint`, `sas`, `sas:signers --set`, `config`, `payout --owner`, `topup`,
 * `smoke [--only gate0] [--mint tusd|circle]`, `alt`, `verify [--recording]` (`--help` lists the
 * flags). Every step is idempotent: it reads `deployments/devnet.json` and chain state, does only
 * what is missing, verifies, rewrites the file (public data only) and prints the `NEXT_PUBLIC_*`
 * lines. Keys come from `$BIDO_KEYS_DIR` (refused inside the repository); the RPC from
 * `SOLANA_RPC_URL` (devnet only; `solana/.env.local` is loaded when present). The planners and
 * builders live in `chain-setup-lib.ts`. Other scripts import `setAuthorizedSigners` and `topup`.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { MINT_SIZE, getAssociatedTokenAddressSync } from "@solana/spl-token";
import {
  Connection,
  Keypair,
  LAMPORTS_PER_SOL,
  PublicKey,
  TransactionMessage,
  VersionedTransaction,
  type TransactionError,
  type TransactionInstruction,
} from "@solana/web3.js";

import {
  CIRCLE_DEVNET_USDC_MINT,
  PROGRAM_ERROR_CODES,
  SAS_CREDENTIAL_NAME,
  SAS_SCHEMA_NAME,
  SAS_SCHEMA_VERSION,
  type ProgramErrorName,
} from "../src/contracts";
import { buildApprovalInstructions } from "../src/client/approval-tx";
import {
  createPoIx,
  decodeConfig,
  decodePoEscrow,
  fundIx,
  payoutRefMemoIx,
  releaseIx,
  type PoEscrowAccount,
} from "../src/client/escrow";
import { computeEvidenceHash } from "../src/client/hashes";
import {
  configPda,
  credentialPda,
  escrowPda,
  eventAuthorityPda,
  schemaPda,
} from "../src/client/pdas";

import {
  FUNDED_KEYS,
  KEY_NAMES,
  MIN_SOL_LAMPORTS,
  PAYOUT_OWNER_LABEL,
  PUBLIC_DEVNET_RPC_URL,
  SMOKE_TOTALS,
  SetupError,
  TUSD_DECIMALS,
  TUSD_SUPPLY,
  USAGE,
  UsageError,
  altAddresses,
  assertDevnetGenesis,
  assertDevnetRpcUrl,
  assertKeyPathOutsideRepo,
  changeAuthorizedSignersIx,
  computeBudgetIxs,
  createCredentialIx,
  createMintIxs,
  createPayoutAtaIx,
  createSchemaIx,
  cuLimitFor,
  customErrorCode,
  decodeCredential,
  decodeLookupTable,
  decodeMint,
  decodeProgramAccount,
  decodeProgramData,
  decodeSchema,
  decodeTokenAccount,
  emptyDeployment,
  envLines,
  explorerTxUrl,
  finishMintIxs,
  formatTokenAmount,
  initConfigIx,
  keyFilePath,
  lookupTableIxs,
  mergeMeasurements,
  messageOf,
  newSmokePoId,
  parseArgs,
  parseDeployment,
  parseKeypairFile,
  planAlt,
  planConfig,
  planMint,
  planPayout,
  planSas,
  planSmoke,
  planTopUp,
  serializeDeployment,
  signerLabels,
  signersNeedChange,
  smokeHashHex,
  smokePoArgs,
  topLevelUnits,
  topUpIxs,
  updateConfigIx,
  verifyDeployment,
  type AccountSnapshot,
  type ChainReader,
  type Command,
  type Deployment,
  type KeyName,
  type Measurements,
  type SignerEntry,
  type WantedConfig,
} from "./chain-setup-lib";

const SCRIPT_FILE = fileURLToPath(import.meta.url);
const SOLANA_ROOT = path.resolve(path.dirname(SCRIPT_FILE), "..");
const REPO_ROOT = path.resolve(SOLANA_ROOT, "..");
const DEPLOYMENT_FILE = path.join(SOLANA_ROOT, "deployments", "devnet.json");
const ENV_LOCAL_FILE = path.join(SOLANA_ROOT, ".env.local");

// ---- transactions ----

export interface TxResult {
  signature: string;
  bytes: number;
  units: number;
  logs: string[];
  err: TransactionError | null;
}

/**
 * Signs, sends and confirms one v0 transaction, `payer` paying the fee. `computeUnits` prepends
 * the §3.9 `[SetComputeUnitLimit, SetComputeUnitPrice]` (escrow transactions only). A failed
 * transaction throws unless `expectFailure` (the failed-release scene, sent with `skipPreflight`).
 */
export async function sendTransaction(
  connection: Connection,
  {
    instructions,
    payer,
    signers = [],
    computeUnits,
    expectFailure = false,
  }: {
    instructions: TransactionInstruction[];
    payer: Keypair;
    signers?: Keypair[];
    computeUnits?: number;
    expectFailure?: boolean;
  },
): Promise<TxResult> {
  const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash("confirmed");
  const message = new TransactionMessage({
    payerKey: payer.publicKey,
    recentBlockhash: blockhash,
    instructions:
      computeUnits === undefined
        ? instructions
        : [...computeBudgetIxs(computeUnits), ...instructions],
  }).compileToV0Message();
  const transaction = new VersionedTransaction(message);
  const unique = new Map([payer, ...signers].map((key) => [key.publicKey.toBase58(), key]));
  transaction.sign([...unique.values()]);
  const bytes = transaction.serialize().length;
  const signature = await connection.sendTransaction(transaction, {
    skipPreflight: expectFailure,
    maxRetries: 5,
  });
  const confirmation = await connection.confirmTransaction(
    { signature, blockhash, lastValidBlockHeight },
    "confirmed",
  );
  const fetched = await fetchTransaction(connection, signature);
  const result: TxResult = {
    signature,
    bytes,
    units: fetched?.meta?.computeUnitsConsumed ?? 0,
    logs: fetched?.meta?.logMessages ?? [],
    err: confirmation.value.err,
  };
  if (result.err !== null && !expectFailure) {
    throw new SetupError(
      [
        `transaction failed: ${JSON.stringify(result.err)}`,
        `  ${explorerTxUrl(signature)}`,
        ...result.logs.slice(-12).map((line) => `  ${line}`),
      ].join("\n"),
    );
  }
  return result;
}

async function fetchTransaction(connection: Connection, signature: string) {
  for (let attempt = 0; attempt < 6; attempt++) {
    const transaction = await connection.getTransaction(signature, {
      commitment: "confirmed",
      maxSupportedTransactionVersion: 0,
    });
    if (transaction !== null) return transaction;
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  return null;
}

function snapshotOf(info: {
  owner: PublicKey;
  executable: boolean;
  lamports: number;
  data: Uint8Array;
}): AccountSnapshot {
  return {
    owner: info.owner.toBase58(),
    executable: info.executable,
    lamports: info.lamports,
    data: new Uint8Array(info.data),
  };
}

function connectionReader(connection: Connection): ChainReader {
  return {
    async getAccount(address) {
      const info = await connection.getAccountInfo(new PublicKey(address), "confirmed");
      return info === null ? null : snapshotOf(info);
    },
    getGenesisHash: () => connection.getGenesisHash(),
  };
}

// ---- exports for other scripts ----

/**
 * SAS `ChangeAuthorizedSigners` to exactly `signers` (spec 01 §3.12 `sas:signers`), signed and paid
 * by the issuer; nothing when the credential already has exactly them. Returns the signature, or
 * null when nothing was sent.
 */
export async function setAuthorizedSigners({
  connection,
  issuer,
  credential,
  signers,
}: {
  connection: Connection;
  issuer: Keypair;
  credential: PublicKey;
  signers: readonly string[];
}): Promise<string | null> {
  const read = async () => {
    const info = await connection.getAccountInfo(credential, "confirmed");
    if (info === null) throw new SetupError(`credential ${credential.toBase58()} does not exist`);
    return decodeCredential(snapshotOf(info));
  };
  const current = await read();
  if (current.authority !== issuer.publicKey.toBase58()) {
    throw new SetupError(`credential authority is ${current.authority}, not sas-issuer.json`);
  }
  if (!signersNeedChange(current.signers, signers)) return null;
  const { signature } = await sendTransaction(connection, {
    instructions: [changeAuthorizedSignersIx(issuer.publicKey, credential, signers)],
    payer: issuer,
  });
  const after = await read();
  if (signersNeedChange(after.signers, signers)) {
    throw new SetupError(`credential signers are [${after.signers.join(", ")}] after the change`);
  }
  return signature;
}

/**
 * Brings `to`'s token account up to at least `amount` base units from the treasury (spec 01 §3.12
 * `topup`; spec 11 `plan_top_up`), creating the account if needed; the treasury signs and pays.
 */
export async function topup({
  connection,
  treasury,
  mint,
  to,
  amount,
  decimals = TUSD_DECIMALS,
}: {
  connection: Connection;
  treasury: Keypair;
  mint: PublicKey;
  to: PublicKey;
  amount: bigint;
  decimals?: number;
}): Promise<{ transferred: bigint; balance: bigint; signature: string | null }> {
  const ata = getAssociatedTokenAddressSync(mint, to, true);
  const info = await connection.getAccountInfo(ata, "confirmed");
  const balance =
    info === null
      ? BigInt(0)
      : decodeTokenAccount(ata.toBase58(), snapshotOf(info), "account").amount;
  const transfer = planTopUp(balance, amount);
  if (transfer === BigInt(0)) return { transferred: transfer, balance, signature: null };
  const { signature } = await sendTransaction(connection, {
    instructions: topUpIxs({ treasury: treasury.publicKey, mint, to, amount: transfer, decimals }),
    payer: treasury,
  });
  return { transferred: transfer, balance: balance + transfer, signature };
}

// ---- steps ----

interface Context {
  connection: Connection;
  chain: ChainReader;
  keysDir: string | undefined;
  deployment: Deployment;
}

function log(line = ""): void {
  process.stdout.write(`${line}\n`);
}

function keyFile(ctx: Context, name: KeyName): string {
  return keyFilePath(ctx.keysDir, name, REPO_ROOT);
}

function hasKey(ctx: Context, name: KeyName): boolean {
  return fs.existsSync(keyFile(ctx, name));
}

function loadKey(ctx: Context, name: KeyName): Keypair {
  const file = keyFile(ctx, name);
  if (!fs.existsSync(file)) throw new SetupError(`${name}.json is not in $BIDO_KEYS_DIR`);
  return parseKeypairFile(fs.readFileSync(file, "utf8"), `${name}.json`);
}

function need(d: Deployment, field: keyof Deployment, step: string): string {
  const value = d[field];
  if (typeof value !== "string" || value === "") {
    throw new SetupError(`${field} is not recorded yet; run \`${step}\` first`);
  }
  return value;
}

function printTx(label: string, result: TxResult): void {
  log(`${label}: ${result.signature}`);
  log(`  ${explorerTxUrl(result.signature)}`);
  log(`  ${result.bytes} B, ${result.units} CU`);
}

async function keysCheck(ctx: Context): Promise<void> {
  const problems: string[] = [];
  for (const name of KEY_NAMES) {
    if (!hasKey(ctx, name)) {
      log(`${name.padEnd(12)} (no ${name}.json)`);
      if (FUNDED_KEYS.includes(name)) problems.push(`${name}.json is missing`);
      continue;
    }
    const pubkey = loadKey(ctx, name).publicKey;
    const lamports = await ctx.connection.getBalance(pubkey, "confirmed");
    log(
      `${name.padEnd(12)} ${pubkey.toBase58().padEnd(44)} ${(lamports / LAMPORTS_PER_SOL).toFixed(4)} SOL`,
    );
    if (FUNDED_KEYS.includes(name) && lamports < MIN_SOL_LAMPORTS) {
      problems.push(`${name} holds ${lamports / LAMPORTS_PER_SOL} SOL, below 2 SOL`);
    }
    if (name === "operator" || name === "relayer") {
      const recorded = ctx.deployment[name];
      if (recorded !== "" && recorded !== pubkey.toBase58()) {
        log(
          `  ${name} changed from the recorded ${recorded}; Config follows only after \`config\``,
        );
      }
      ctx.deployment[name] = pubkey.toBase58();
    }
  }
  if (problems.length > 0) throw new SetupError(problems.join("; "));
}

async function readMint(ctx: Context, mint: string) {
  const account = await ctx.chain.getAccount(mint);
  return account === null ? null : decodeMint(mint, account, "tUSD mint");
}

async function mint(ctx: Context): Promise<void> {
  const d = ctx.deployment;
  const treasury = loadKey(ctx, "treasury");
  const owner = treasury.publicKey.toBase58();
  const actions = planMint({
    recordedMint: d.tusdMint,
    mint: d.tusdMint === "" ? null : await readMint(ctx, d.tusdMint),
    treasury: owner,
  });
  if (actions.some((action) => action.kind === "create_mint")) {
    // The mint keypair only signs its own creation; it is never written anywhere.
    const mintKey = Keypair.generate();
    const rentLamports = await ctx.connection.getMinimumBalanceForRentExemption(MINT_SIZE);
    const result = await sendTransaction(ctx.connection, {
      instructions: createMintIxs({
        treasury: treasury.publicKey,
        mint: mintKey.publicKey,
        rentLamports,
      }),
      payer: treasury,
      signers: [mintKey],
    });
    printTx("mint created", result);
    d.tusdMint = mintKey.publicKey.toBase58();
  } else if (actions.length > 0) {
    const result = await sendTransaction(ctx.connection, {
      instructions: finishMintIxs(treasury.publicKey, new PublicKey(d.tusdMint), actions),
      payer: treasury,
    });
    printTx(`mint finished (${actions.map((a) => a.kind).join(", ")})`, result);
  } else {
    log(`tUSD ${d.tusdMint} already complete`);
  }

  const after = planMint({
    recordedMint: d.tusdMint,
    mint: await readMint(ctx, d.tusdMint),
    treasury: owner,
  });
  if (after.length > 0)
    throw new SetupError(`tUSD is still incomplete: ${after.map((a) => a.kind).join(", ")}`);
  const treasuryAta = getAssociatedTokenAddressSync(
    new PublicKey(d.tusdMint),
    treasury.publicKey,
  ).toBase58();
  const ataAccount = await ctx.chain.getAccount(treasuryAta);
  if (ataAccount === null) {
    throw new SetupError(
      `treasury.json has no tUSD account (${treasuryAta}); is it the treasury key?`,
    );
  }
  const balance = decodeTokenAccount(treasuryAta, ataAccount, "treasury tUSD account").amount;
  log(
    `tUSD ${d.tusdMint}: 6 decimals, supply ${formatTokenAmount(TUSD_SUPPLY, TUSD_DECIMALS)}, no mint or freeze authority`,
  );
  log(`treasury ${owner} holds ${formatTokenAmount(balance, TUSD_DECIMALS)} tUSD`);
  d.tusdTreasuryOwner = owner;
  d.tusdSupply = TUSD_SUPPLY.toString();
}

async function sas(ctx: Context, command: Extract<Command, { step: "sas" }>): Promise<void> {
  const d = ctx.deployment;
  const issuer = loadKey(ctx, "sas-issuer");
  const credential = credentialPda(issuer.publicKey, SAS_CREDENTIAL_NAME);
  const schema = schemaPda(credential, SAS_SCHEMA_NAME, SAS_SCHEMA_VERSION);
  const read = async () => {
    const [credentialAccount, schemaAccount] = await Promise.all([
      ctx.chain.getAccount(credential.toBase58()),
      ctx.chain.getAccount(schema.toBase58()),
    ]);
    return {
      credential: credentialAccount === null ? null : decodeCredential(credentialAccount),
      schema: schemaAccount === null ? null : decodeSchema(schemaAccount),
    };
  };
  const plan = (state: Awaited<ReturnType<typeof read>>) =>
    planSas({
      issuer: issuer.publicKey.toBase58(),
      credentialAddress: credential.toBase58(),
      ...state,
    });

  const before = await read();
  const actions = plan(before);
  const instructions: TransactionInstruction[] = [];
  let initialSigners: SignerEntry[] = [];
  if (actions.some((action) => action.kind === "create_credential")) {
    initialSigners = command.signers ?? defaultSmokeSigners(ctx);
    instructions.push(
      createCredentialIx(
        issuer.publicKey,
        initialSigners.map((e) => e.pubkey),
      ),
    );
  } else if (command.signers !== undefined) {
    log("the credential exists; --signers is ignored (use `sas:signers --set` to change signers)");
  }
  if (actions.some((action) => action.kind === "create_schema")) {
    instructions.push(createSchemaIx(issuer.publicKey, credential));
  }
  if (instructions.length > 0) {
    printTx(
      `sas (${actions.map((a) => a.kind).join(", ")})`,
      await sendTransaction(ctx.connection, {
        instructions,
        payer: issuer,
      }),
    );
  } else {
    log("credential and schema already match §3.12");
  }

  const after = await read();
  if (plan(after).length > 0 || after.credential === null) {
    throw new SetupError("the credential or schema is still missing after the transaction");
  }
  d.sasIssuer = issuer.publicKey.toBase58();
  d.sasCredential = credential.toBase58();
  d.sasSchema = schema.toBase58();
  if (initialSigners.length > 0) {
    d.smokeSigners = initialSigners.map((entry) => entry.pubkey);
    d.sasSignerLabels = signerLabels(initialSigners, d.sasSignerLabels);
  } else {
    d.sasSignerLabels = signerLabels(
      after.credential.signers.map((pubkey) => ({ pubkey })),
      d.sasSignerLabels,
    );
  }
  log(`credential ${d.sasCredential} signers: ${after.credential.signers.join(", ")}`);
  log(`schema ${d.sasSchema}`);
}

/** The initial credential signers: the recorded smoke signers, else `smoke-gate0` and `smoke-gate1`. */
function defaultSmokeSigners(ctx: Context): SignerEntry[] {
  const d = ctx.deployment;
  if (d.smokeSigners.length > 0) {
    return d.smokeSigners.map((pubkey) => ({ pubkey, label: d.sasSignerLabels[pubkey] }));
  }
  const names: KeyName[] = ["smoke-gate0", "smoke-gate1"];
  if (!names.every((name) => hasKey(ctx, name))) {
    throw new UsageError("pass --signers <pk,pk> (the smoke-gate0 and smoke-gate1 public keys)");
  }
  return names.map((name) => ({ pubkey: loadKey(ctx, name).publicKey.toBase58(), label: name }));
}

async function sasSigners(ctx: Context, command: Extract<Command, { step: "sas:signers" }>) {
  const d = ctx.deployment;
  const issuer = loadKey(ctx, "sas-issuer");
  const credential = need(d, "sasCredential", "sas");
  if (credentialPda(issuer.publicKey, SAS_CREDENTIAL_NAME).toBase58() !== credential) {
    throw new SetupError(`sas-issuer.json does not derive the recorded credential ${credential}`);
  }
  const signers = command.set.map((entry) => entry.pubkey);
  const signature = await setAuthorizedSigners({
    connection: ctx.connection,
    issuer,
    credential: new PublicKey(credential),
    signers,
  });
  if (signature === null) log("the credential already has exactly these signers");
  else log(`signers set: ${signature}\n  ${explorerTxUrl(signature)}`);
  d.sasSignerLabels = signerLabels(command.set, d.sasSignerLabels);
  const smoke = signers.filter((pubkey) => d.smokeSigners.includes(pubkey));
  log(
    `signers: ${command.set.map((e) => `${d.sasSignerLabels[e.pubkey]}=${e.pubkey}`).join(", ")}`,
  );
  if (smoke.length > 0)
    log(`smoke signers still authorised: ${smoke.join(", ")} (remove them at the freeze)`);
}

async function config(ctx: Context): Promise<void> {
  const d = ctx.deployment;
  const admin = loadKey(ctx, "admin");
  const programId = new PublicKey(need(d, "programId", "the program id"));
  const tusdMint = need(d, "tusdMint", "mint");
  const credential = need(d, "sasCredential", "sas");
  const schema = need(d, "sasSchema", "sas");
  const operator = d.operator !== "" ? d.operator : loadKey(ctx, "operator").publicKey.toBase58();

  const program = await ctx.chain.getAccount(programId.toBase58());
  if (program === null) throw new SetupError(`program ${programId.toBase58()} is not deployed`);
  const programData = await ctx.chain.getAccount(decodeProgramAccount(program));
  if (programData === null)
    throw new SetupError("the program's ProgramData account does not exist");
  const { upgradeAuthority } = decodeProgramData(programData);
  if (upgradeAuthority !== admin.publicKey.toBase58()) {
    throw new SetupError(`admin.json is not the upgrade authority (${upgradeAuthority ?? "none"})`);
  }

  const wanted: WantedConfig = {
    admin: upgradeAuthority,
    operator,
    allowedMints: [tusdMint, CIRCLE_DEVNET_USDC_MINT],
    sasCredential: credential,
    sasSchema: schema,
  };
  const readConfig = async () => {
    const account = await ctx.chain.getAccount(configPda(programId).toBase58());
    return account === null ? null : decodeConfig(account.data);
  };
  const plan = planConfig(wanted, await readConfig());
  if (plan.kind === "init") {
    const instruction = initConfigIx({
      programId,
      admin: admin.publicKey,
      operator: new PublicKey(operator),
      allowedMints: [new PublicKey(tusdMint), new PublicKey(CIRCLE_DEVNET_USDC_MINT)],
      credential: new PublicKey(credential),
      schema: new PublicKey(schema),
    });
    printTx(
      "init_config",
      await sendTransaction(ctx.connection, { instructions: [instruction], payer: admin }),
    );
  } else if (plan.kind === "update") {
    const instruction = updateConfigIx({
      programId,
      admin: admin.publicKey,
      args: plan.args,
      credential: new PublicKey(credential),
      schema: new PublicKey(schema),
    });
    const changed = Object.entries(plan.args)
      .filter(([, value]) => value !== null)
      .map(([key]) => key);
    printTx(
      `update_config (${changed.length > 0 ? changed.join(", ") : "admin only"})`,
      await sendTransaction(ctx.connection, { instructions: [instruction], payer: admin }),
    );
  } else {
    log("Config already matches");
  }
  if (planConfig(wanted, await readConfig()).kind !== "none") {
    throw new SetupError("Config still differs after the transaction");
  }
  d.upgradeAuthority = upgradeAuthority;
  d.configPda = configPda(programId).toBase58();
  d.eventAuthority = eventAuthorityPda(programId).toBase58();
  d.operator = operator;
  log(`Config ${d.configPda}: operator ${operator}, mints [${wanted.allowedMints.join(", ")}]`);
}

async function payout(ctx: Context, command: Extract<Command, { step: "payout" }>) {
  const d = ctx.deployment;
  const tusdMint = need(d, "tusdMint", "mint");
  const owner = new PublicKey(command.owner);
  const ata = getAssociatedTokenAddressSync(new PublicKey(tusdMint), owner, true).toBase58();
  const read = async () => {
    const account = await ctx.chain.getAccount(ata);
    return account === null ? null : decodeTokenAccount(ata, account, "payout tUSD account");
  };
  if (d.payoutOwner !== "" && d.payoutOwner !== command.owner) {
    log(`payout owner changes from ${d.payoutOwner}; existing escrows keep their payout`);
  }
  if (planPayout({ ataAddress: ata, ata: await read(), owner: command.owner, mint: tusdMint })) {
    const payer = loadKey(ctx, "deploy-payer");
    printTx(
      "payout account",
      await sendTransaction(ctx.connection, {
        instructions: [createPayoutAtaIx(payer.publicKey, owner, new PublicKey(tusdMint))],
        payer,
      }),
    );
  } else {
    log(`payout tUSD account ${ata} already exists`);
  }
  if ((await read()) === null)
    throw new SetupError(`payout tUSD account ${ata} still does not exist`);
  d.payoutOwner = command.owner;
  d.payoutOwnerLabel = PAYOUT_OWNER_LABEL;
  log(`payout owner ${d.payoutOwner} (${d.payoutOwnerLabel}), tUSD account ${ata}`);
}

async function topupStep(ctx: Context, command: Extract<Command, { step: "topup" }>) {
  const tusdMint = need(ctx.deployment, "tusdMint", "mint");
  const result = await topup({
    connection: ctx.connection,
    treasury: loadKey(ctx, "treasury"),
    mint: new PublicKey(tusdMint),
    to: new PublicKey(command.to),
    amount: command.amount,
  });
  const amount = (units: bigint) => `${formatTokenAmount(units, TUSD_DECIMALS)} tUSD`;
  if (result.signature === null) log(`${command.to} already holds ${amount(result.balance)}`);
  else {
    log(
      `sent ${amount(result.transferred)}: ${result.signature}\n  ${explorerTxUrl(result.signature)}`,
    );
    log(`${command.to} now holds ${amount(result.balance)}`);
  }
}

interface SmokeKeys {
  operator: Keypair;
  relayer: Keypair;
  buyer: Keypair;
  gates: [Keypair, Keypair];
}

interface SmokeRun {
  programId: PublicKey;
  mint: PublicKey;
  total: bigint;
  payout: PublicKey;
  credential: PublicKey;
  keys: SmokeKeys;
  /** The maximum of each measurement in this run. */
  seen: Measurements;
}

function see(
  run: SmokeRun,
  cu: Partial<Measurements["cu"]>,
  txBytes: Partial<Measurements["txBytes"]> = {},
) {
  run.seen = mergeMeasurements(run.seen, { cu, txBytes });
}

async function readEscrow(ctx: Context, escrow: PublicKey): Promise<PoEscrowAccount | null> {
  const account = await ctx.chain.getAccount(escrow.toBase58());
  return account === null ? null : decodePoEscrow(account.data);
}

/** Runs the due smoke steps of one escrow, re-reading it after each; returns the signatures. */
async function driveSmokeEscrow(
  ctx: Context,
  run: SmokeRun,
  chainPoId: string,
  until: "gate0" | "fund" | undefined,
): Promise<{ escrow: PublicKey; signatures: string[] }> {
  const { programId, keys } = run;
  const [escrow] = escrowPda(programId, keys.buyer.publicKey, chainPoId);
  const signatures: string[] = [];
  for (;;) {
    const state = await readEscrow(ctx, escrow);
    const [action] = planSmoke(state, until);
    if (action === undefined) return { escrow, signatures };
    let result: TxResult;
    if (action === "create_po") {
      const { args, payoutRefHex } = smokePoArgs({
        chainPoId,
        total: run.total,
        gateAttesters: [keys.gates[0].publicKey, keys.gates[1].publicKey],
        nowSeconds: Date.now() / 1_000,
      });
      result = await sendTransaction(ctx.connection, {
        instructions: [
          createPoIx({
            programId,
            operator: keys.operator.publicKey,
            payer: keys.relayer.publicKey,
            buyer: keys.buyer.publicKey,
            payout: run.payout,
            mint: run.mint,
            credential: run.credential,
            args,
          }),
          payoutRefMemoIx(payoutRefHex),
        ],
        payer: keys.relayer,
        signers: [keys.operator],
        computeUnits: cuLimitFor("createPo"),
      });
      see(run, { createPo: result.units }, { create_po: result.bytes });
    } else if (action === "fund" && state !== null) {
      result = await sendTransaction(ctx.connection, {
        instructions: [
          fundIx({
            programId,
            escrow,
            state,
            buyerToken: getAssociatedTokenAddressSync(run.mint, keys.buyer.publicKey, true),
          }),
        ],
        payer: keys.relayer,
        signers: [keys.buyer],
        computeUnits: cuLimitFor("fund"),
      });
      see(run, { fund: result.units });
    } else if (state !== null) {
      const gate = action === "approve_gate0" ? 0 : 1;
      const approver = keys.gates[gate];
      result = await sendTransaction(ctx.connection, {
        instructions: buildApprovalInstructions({
          programId,
          escrow,
          state,
          gate,
          approver: approver.publicKey,
          payer: keys.relayer.publicKey,
          evidenceHash: computeEvidenceHash(chainPoId, gate, [
            smokeHashHex(`bido-smoke:evidence:${chainPoId}:${gate}`),
          ]),
        }),
        payer: keys.relayer,
        signers: [approver],
        computeUnits: cuLimitFor("approval"),
      });
      const releases = topLevelUnits(result.logs, programId.toBase58());
      see(
        run,
        { approval: result.units, release: Math.max(0, ...releases) },
        gate === 1 ? { approve_ppap: result.bytes } : {},
      );
    } else {
      throw new SetupError(`smoke escrow ${chainPoId} vanished before ${action}`);
    }
    printTx(`${chainPoId} ${action}`, result);
    signatures.push(result.signature);
  }
}

/** A release that must fail with `expected`, sent with `skipPreflight` so it lands on chain. */
async function failedRelease(
  ctx: Context,
  run: SmokeRun,
  label: string,
  instruction: TransactionInstruction,
  expected: ProgramErrorName,
): Promise<void> {
  const result = await sendTransaction(ctx.connection, {
    instructions: [instruction],
    payer: run.keys.relayer,
    computeUnits: cuLimitFor("release"),
    expectFailure: true,
  });
  printTx(label, result);
  const code = customErrorCode(result.err);
  if (code !== PROGRAM_ERROR_CODES[expected]) {
    throw new SetupError(
      `${label} should fail with ${expected} (${PROGRAM_ERROR_CODES[expected]}), got ${JSON.stringify(result.err)}`,
    );
  }
  log(`  failed as expected: ${expected} (${code})`);
}

async function smoke(ctx: Context, command: Extract<Command, { step: "smoke" }>): Promise<void> {
  const d = ctx.deployment;
  const programId = new PublicKey(need(d, "programId", "the program id"));
  const mintAddress =
    command.mint === "circle" ? CIRCLE_DEVNET_USDC_MINT : need(d, "tusdMint", "mint");
  const credential = need(d, "sasCredential", "sas");
  const payoutOwner = need(d, "payoutOwner", "payout");
  const keys: SmokeKeys = {
    operator: loadKey(ctx, "operator"),
    relayer: loadKey(ctx, "relayer"),
    buyer: loadKey(ctx, "smoke-buyer"),
    gates: [loadKey(ctx, "smoke-gate0"), loadKey(ctx, "smoke-gate1")],
  };
  const run: SmokeRun = {
    programId,
    mint: new PublicKey(mintAddress),
    total: SMOKE_TOTALS[command.mint],
    payout: new PublicKey(payoutOwner),
    credential: new PublicKey(credential),
    keys,
    seen: emptyDeployment().measurements,
  };

  const configAccount = await ctx.chain.getAccount(configPda(programId).toBase58());
  if (configAccount === null) throw new SetupError("Config does not exist; run `config` first");
  const configState = decodeConfig(configAccount.data);
  if (configState.operator !== keys.operator.publicKey.toBase58()) {
    throw new SetupError(`operator.json is not Config.operator (${configState.operator})`);
  }
  if (!configState.allowedMints.includes(mintAddress)) {
    throw new SetupError(`Config does not allow mint ${mintAddress}`);
  }
  const credentialAccount = await ctx.chain.getAccount(credential);
  if (credentialAccount === null) throw new SetupError(`credential ${credential} does not exist`);
  const authorised = decodeCredential(credentialAccount).signers;
  const missing = keys.gates
    .map((k) => k.publicKey.toBase58())
    .filter((pk) => !authorised.includes(pk));
  if (missing.length > 0) {
    throw new SetupError(
      `smoke gate keys are not credential signers: ${missing.join(", ")} (run \`sas:signers\`)`,
    );
  }

  const chainPoId = command.po ?? newSmokePoId();
  const [firstEscrow] = escrowPda(programId, keys.buyer.publicKey, chainPoId);
  const firstState = await readEscrow(ctx, firstEscrow);
  const failedReleases = command.only === undefined && command.mint === "tusd";
  const fundings =
    (planSmoke(firstState, command.only).includes("fund") ? 1 : 0) + (failedReleases ? 1 : 0);
  const buyerAta = getAssociatedTokenAddressSync(run.mint, keys.buyer.publicKey, true).toBase58();
  const buyerAccount = await ctx.chain.getAccount(buyerAta);
  const buyerBalance =
    buyerAccount === null
      ? BigInt(0)
      : decodeTokenAccount(buyerAta, buyerAccount, "smoke-buyer account").amount;
  const needed = run.total * BigInt(fundings);
  if (buyerBalance < needed) {
    throw new SetupError(
      `smoke-buyer holds ${formatTokenAmount(buyerBalance, 6)} of mint ${mintAddress}, needs ${formatTokenAmount(needed, 6)}` +
        (command.mint === "tusd"
          ? `; run \`topup --to ${keys.buyer.publicKey.toBase58()} --amount ${formatTokenAmount(needed, TUSD_DECIMALS)}\``
          : ""),
    );
  }

  const first = await driveSmokeEscrow(ctx, run, chainPoId, command.only);
  const firstAfter = await readEscrow(ctx, first.escrow);
  if (firstAfter === null) throw new SetupError(`smoke escrow ${chainPoId} does not exist`);
  if (command.only === undefined && firstAfter.status !== "Completed") {
    throw new SetupError(`smoke escrow ${chainPoId} is ${firstAfter.status}, not Completed`);
  }
  log(`${chainPoId} (${first.escrow.toBase58()}) is ${firstAfter.status}`);

  if (failedReleases) {
    const secondId = newSmokePoId();
    const second = await driveSmokeEscrow(ctx, run, secondId, "fund");
    const secondState = await readEscrow(ctx, second.escrow);
    if (secondState === null || secondState.status !== "Funded") {
      throw new SetupError(`second smoke escrow ${secondId} is not Funded`);
    }
    const lastMilestone = secondState.milestones.length - 1;
    await failedRelease(
      ctx,
      run,
      `${secondId} release(${lastMilestone}) without an attestation`,
      releaseIx({ programId, escrow: second.escrow, state: secondState, milestone: lastMilestone }),
      "AttestationMissing",
    );
    await failedRelease(
      ctx,
      run,
      `${secondId} release(${lastMilestone}) with ${chainPoId}'s attestation`,
      releaseIx({
        programId,
        escrow: second.escrow,
        state: secondState,
        milestone: lastMilestone,
        attestation: new PublicKey(
          firstAfter.gates[secondState.milestones[lastMilestone].gate].attestation,
        ),
      }),
      "AttestationAddressMismatch",
    );
  }

  if (command.mint === "circle" && firstAfter.status === "Completed") {
    d.circleProof = { chainPoId, escrow: first.escrow.toBase58(), signatures: first.signatures };
  }
  d.measurements = mergeMeasurements(d.measurements, run.seen);
  log(`measurements: ${JSON.stringify(d.measurements)}`);
}

async function alt(ctx: Context): Promise<void> {
  const d = ctx.deployment;
  const wanted = altAddresses(d);
  let existing: string[] | null = null;
  if (d.alt !== undefined) {
    const table = await ctx.chain.getAccount(d.alt);
    if (table === null) throw new SetupError(`the recorded lookup table ${d.alt} does not exist`);
    existing = decodeLookupTable(d.alt, table);
  }
  const plan = planAlt({ measurements: d.measurements, wanted, existing });
  if (plan.kind === "unmeasured") {
    throw new SetupError("measurements.txBytes are not recorded; run `smoke` first");
  }
  if (plan.kind === "not_needed") {
    log(
      `no lookup table needed: create_po ${d.measurements.txBytes.create_po} B, approve_ppap ${d.measurements.txBytes.approve_ppap} B`,
    );
    return;
  }
  if (plan.kind === "none") {
    log(`lookup table ${d.alt ?? ""} already holds every static address`);
    return;
  }
  const payer = loadKey(ctx, "deploy-payer");
  const { table, instructions } = lookupTableIxs({
    authority: payer.publicKey,
    recentSlot: await ctx.connection.getSlot("finalized"),
    table: d.alt === undefined ? null : new PublicKey(d.alt),
    addresses: plan.addresses,
  });
  printTx(
    `lookup table ${plan.kind}`,
    await sendTransaction(ctx.connection, { instructions, payer }),
  );
  const account = await ctx.chain.getAccount(table.toBase58());
  const addresses = account === null ? [] : decodeLookupTable(table.toBase58(), account);
  const absent = wanted.filter((a) => !addresses.includes(a));
  if (absent.length > 0)
    throw new SetupError(`lookup table ${table.toBase58()} lacks ${absent.join(", ")}`);
  d.alt = table.toBase58();
  log(`lookup table ${d.alt}; set NEXT_PUBLIC_BIDO_ALT and BIDO_ALT (report it to spec 00)`);
}

async function verify(ctx: Context, command: Extract<Command, { step: "verify" }>) {
  const problems = await verifyDeployment(ctx.deployment, ctx.chain, command);
  for (const problem of problems) log(`✗ ${problem}`);
  if (problems.length > 0) throw new SetupError(`verify: ${problems.length} problem(s)`);
  log(`verify${command.recording ? " --recording" : ""}: deployments/devnet.json matches devnet`);
}

async function runStep(ctx: Context, command: Exclude<Command, { step: "help" }>): Promise<void> {
  switch (command.step) {
    case "keys:check":
      return keysCheck(ctx);
    case "mint":
      return mint(ctx);
    case "sas":
      return sas(ctx, command);
    case "sas:signers":
      return sasSigners(ctx, command);
    case "config":
      return config(ctx);
    case "payout":
      return payout(ctx, command);
    case "topup":
      return topupStep(ctx, command);
    case "smoke":
      return smoke(ctx, command);
    case "alt":
      return alt(ctx);
    case "verify":
      return verify(ctx, command);
  }
}

// ---- main ----

async function main(argv: string[]): Promise<number> {
  let command: Command;
  try {
    command = parseArgs(argv);
  } catch (error) {
    process.stderr.write(`chain-setup: ${messageOf(error)}\n\n${USAGE}\n`);
    return 2;
  }
  if (command.step === "help") {
    log(USAGE);
    return 0;
  }
  try {
    if (fs.existsSync(ENV_LOCAL_FILE)) process.loadEnvFile(ENV_LOCAL_FILE);
    const keysDir = process.env.BIDO_KEYS_DIR;
    if (keysDir !== undefined && keysDir !== "") assertKeyPathOutsideRepo(keysDir, REPO_ROOT);
    const rpcUrl = process.env.SOLANA_RPC_URL ?? PUBLIC_DEVNET_RPC_URL;
    const url = assertDevnetRpcUrl(rpcUrl);
    const deployment = parseDeployment(fs.readFileSync(DEPLOYMENT_FILE, "utf8"));
    const connection = new Connection(url.toString(), "confirmed");
    assertDevnetGenesis(await connection.getGenesisHash());
    log(`devnet via ${url.hostname}`);

    const ctx: Context = { connection, chain: connectionReader(connection), keysDir, deployment };
    await runStep(ctx, command);
    fs.writeFileSync(DEPLOYMENT_FILE, serializeDeployment(ctx.deployment));
    log();
    for (const line of envLines(ctx.deployment)) log(line);
    return 0;
  } catch (error) {
    process.stderr.write(`chain-setup: ${messageOf(error)}\n`);
    if (error instanceof UsageError) process.stderr.write(`\n${USAGE}\n`);
    return 1;
  }
}

if (process.argv[1] !== undefined && path.resolve(process.argv[1]) === SCRIPT_FILE) {
  void main(process.argv.slice(2)).then((code) => {
    process.exitCode = code;
  });
}
