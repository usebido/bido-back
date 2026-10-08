// @vitest-environment node
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { getCredentialEncoder, getSchemaEncoder } from "@solana/attestation";
import {
  ACCOUNT_SIZE,
  AccountLayout,
  AccountState,
  MINT_SIZE,
  MintLayout,
  TOKEN_PROGRAM_ID,
  getAssociatedTokenAddressSync,
} from "@solana/spl-token";
import { Keypair, PublicKey } from "@solana/web3.js";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
  CHAIN_PO_ID_RE,
  CIRCLE_DEVNET_USDC_MINT,
  CU_LIMITS,
  MAX_TX_BYTES,
  SAS_CREDENTIAL_NAME,
  SAS_PROGRAM_ID,
  SAS_SCHEMA_FIELDS,
  SAS_SCHEMA_LAYOUT,
  SAS_SCHEMA_NAME,
  SAS_SCHEMA_VERSION,
} from "../src/contracts";
import idl from "../idl/bido_escrow.json";
import { escrowCoder, type ConfigAccount, type PoEscrowAccount } from "../src/client/escrow";
import { sha256 } from "../src/client/hashes";
import {
  configPda,
  credentialPda,
  escrowPda,
  eventAuthorityPda,
  payoutAta,
  schemaPda,
} from "../src/client/pdas";

import {
  DEVNET_GENESIS_HASH,
  PAYOUT_OWNER_LABEL,
  SAS_SCHEMA_DESCRIPTION,
  SetupError,
  TUSD_SUPPLY,
  UsageError,
  altAddresses,
  assertDevnetGenesis,
  assertDevnetRpcUrl,
  assertKeyPathOutsideRepo,
  changeAuthorizedSignersIx,
  computeBudgetIxs,
  createCredentialIx,
  createMintIxs,
  createSchemaIx,
  cuLimitFor,
  customErrorCode,
  decodeCredential,
  decodeLookupTable,
  decodeMint,
  decodeSchema,
  decodeTokenAccount,
  emptyDeployment,
  envLines,
  formatTokenAmount,
  initConfigIx,
  keyFilePath,
  mergeMeasurements,
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
  programDataAddress,
  programHash,
  recordingSigners,
  serializeDeployment,
  signerLabels,
  signersNeedChange,
  smokePoArgs,
  topLevelUnits,
  updateConfigIx,
  verifyDeployment,
  type AccountSnapshot,
  type ChainReader,
  type Deployment,
  type Measurements,
  type SchemaState,
  type WantedConfig,
} from "./chain-setup-lib";

const solanaRoot = fileURLToPath(new URL("..", import.meta.url));
const repoRoot = path.resolve(solanaRoot, "..");
const deploymentFile = path.join(solanaRoot, "deployments", "devnet.json");

const PROGRAM_ID = "7992GKVq51ChvsVJQ9GFto2aBozUGuMcuY93SUSxexJw";
const UPGRADEABLE_LOADER = "BPFLoaderUpgradeab1e11111111111111111111111";

/** Throwaway keys from documented seeds: `sha256("chain-setup-test:<label>")`. */
function key(label: string): Keypair {
  return Keypair.fromSeed(sha256(new TextEncoder().encode(`chain-setup-test:${label}`)));
}
const pk = (label: string) => key(label).publicKey.toBase58();

// ---- the fake chain ----

class FakeChain implements ChainReader {
  accounts = new Map<string, AccountSnapshot>();
  genesis = DEVNET_GENESIS_HASH;

  async getAccount(address: string) {
    return this.accounts.get(address) ?? null;
  }

  async getGenesisHash() {
    return this.genesis;
  }

  put(address: string, owner: string, data: Uint8Array, executable = false) {
    this.accounts.set(address, { owner, executable, lamports: 1_000_000, data });
  }

  putMint(
    address: string,
    {
      supply,
      decimals = 6,
      mintAuthority = null,
      freezeAuthority = null,
    }: {
      supply: bigint;
      decimals?: number;
      mintAuthority?: string | null;
      freezeAuthority?: string | null;
    },
  ) {
    const data = Buffer.alloc(MINT_SIZE);
    MintLayout.encode(
      {
        mintAuthorityOption: mintAuthority === null ? 0 : 1,
        mintAuthority: mintAuthority === null ? PublicKey.default : new PublicKey(mintAuthority),
        supply,
        decimals,
        isInitialized: true,
        freezeAuthorityOption: freezeAuthority === null ? 0 : 1,
        freezeAuthority:
          freezeAuthority === null ? PublicKey.default : new PublicKey(freezeAuthority),
      },
      data,
    );
    this.put(address, TOKEN_PROGRAM_ID.toBase58(), data);
  }

  putTokenAccount(
    address: string,
    { mint, owner, amount }: { mint: string; owner: string; amount: bigint },
  ) {
    const data = Buffer.alloc(ACCOUNT_SIZE);
    AccountLayout.encode(
      {
        mint: new PublicKey(mint),
        owner: new PublicKey(owner),
        amount,
        delegateOption: 0,
        delegate: PublicKey.default,
        state: AccountState.Initialized,
        isNativeOption: 0,
        isNative: BigInt(0),
        delegatedAmount: BigInt(0),
        closeAuthorityOption: 0,
        closeAuthority: PublicKey.default,
      },
      data,
    );
    this.put(address, TOKEN_PROGRAM_ID.toBase58(), data);
  }

  putAta(owner: string, mint: string, amount = BigInt(0)) {
    const address = payoutAta(new PublicKey(owner), new PublicKey(mint)).toBase58();
    this.putTokenAccount(address, { mint, owner, amount });
    return address;
  }

  putCredential(address: string, authority: string, signers: string[], name = SAS_CREDENTIAL_NAME) {
    const data = getCredentialEncoder().encode({
      discriminator: 0,
      authority: authority as never,
      name: new TextEncoder().encode(name),
      authorizedSigners: signers as never[],
    });
    this.put(address, SAS_PROGRAM_ID, new Uint8Array(data));
  }

  putSchema(address: string, schema: SchemaState) {
    const data = getSchemaEncoder().encode({
      discriminator: 1,
      credential: schema.credential as never,
      name: schema.name,
      description: schema.description,
      layout: schema.layout,
      fieldNames: schema.fieldNames,
      isPaused: schema.isPaused,
      version: schema.version,
    });
    this.put(address, SAS_PROGRAM_ID, new Uint8Array(data));
  }

  async putConfig(config: ConfigAccount) {
    const data = await escrowCoder.accounts.encode("Config", {
      admin: new PublicKey(config.admin),
      operator: new PublicKey(config.operator),
      allowed_mints: config.allowedMints.map((m) => new PublicKey(m)),
      sas_credential: new PublicKey(config.sasCredential),
      sas_schema: new PublicKey(config.sasSchema),
      bump: config.bump,
    });
    this.put(configPda(new PublicKey(PROGRAM_ID)).toBase58(), PROGRAM_ID, new Uint8Array(data));
  }

  /** The upgradeable program account and its `ProgramData` (ELF plus zero padding). */
  putProgram(authority: string | null, elf: Uint8Array) {
    const dataAddress = programDataAddress(PROGRAM_ID);
    const program = Buffer.alloc(36);
    program.writeUInt32LE(2, 0);
    new PublicKey(dataAddress).toBuffer().copy(program, 4);
    this.put(PROGRAM_ID, UPGRADEABLE_LOADER, program, true);
    const data = Buffer.alloc(45 + elf.length + 64);
    data.writeUInt32LE(3, 0);
    data.writeBigUInt64LE(BigInt(508252016), 4);
    if (authority !== null) {
      data[12] = 1;
      new PublicKey(authority).toBuffer().copy(data, 13);
    }
    Buffer.from(elf).copy(data, 45);
    this.put(dataAddress, UPGRADEABLE_LOADER, data);
  }

  putLookupTable(address: string, authority: string, addresses: string[]) {
    const data = Buffer.alloc(56 + 32 * addresses.length);
    data.writeUInt32LE(1, 0);
    data.writeBigUInt64LE(BigInt("18446744073709551615"), 4);
    data[21] = 1;
    new PublicKey(authority).toBuffer().copy(data, 22);
    addresses.forEach((a, i) => new PublicKey(a).toBuffer().copy(data, 56 + 32 * i));
    this.put(address, "AddressLookupTab1e1111111111111111111111111", data);
  }
}

// ---- a complete devnet world ----

const ELF = Uint8Array.from([0x7f, 0x45, 0x4c, 0x46, 1, 2, 3, 0, 4]);

function schemaOf(credential: string): SchemaState {
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

/** A finished setup: every step done, smoke signers still authorised, the build hash recorded. */
async function completeWorld(): Promise<{ chain: FakeChain; deployment: Deployment }> {
  const program = new PublicKey(PROGRAM_ID);
  const tusdMint = pk("tusd-mint");
  const issuer = key("sas-issuer").publicKey;
  const credential = credentialPda(issuer, SAS_CREDENTIAL_NAME).toBase58();
  const schema = schemaPda(
    new PublicKey(credential),
    SAS_SCHEMA_NAME,
    SAS_SCHEMA_VERSION,
  ).toBase58();
  const signers = [pk("smoke-gate0"), pk("smoke-gate1"), pk("buyer-approver"), pk("sqe")];
  const deployment: Deployment = {
    ...emptyDeployment(),
    programId: PROGRAM_ID,
    upgradeAuthority: pk("admin"),
    configPda: configPda(program).toBase58(),
    eventAuthority: eventAuthorityPda(program).toBase58(),
    operator: pk("operator"),
    relayer: pk("relayer"),
    tusdMint,
    tusdTreasuryOwner: pk("treasury"),
    sasIssuer: issuer.toBase58(),
    sasCredential: credential,
    sasSchema: schema,
    sasSignerLabels: {
      [signers[0]]: "smoke-gate0",
      [signers[1]]: "smoke-gate1",
      [signers[2]]: "buyer approver",
      [signers[3]]: "SQE",
    },
    smokeSigners: [signers[0], signers[1]],
    payoutOwner: pk("payout-owner"),
    build: {
      commit: "abc1234",
      executableHash: programHash(ELF),
      solanaVerifyVersion: "0.4.4",
      frozenAt: "",
    },
  };
  const chain = new FakeChain();
  chain.putProgram(pk("admin"), ELF);
  await chain.putConfig({
    admin: pk("admin"),
    operator: pk("operator"),
    allowedMints: [tusdMint, CIRCLE_DEVNET_USDC_MINT],
    sasCredential: credential,
    sasSchema: schema,
    bump: 255,
  });
  chain.putMint(tusdMint, { supply: TUSD_SUPPLY });
  chain.putAta(pk("treasury"), tusdMint, TUSD_SUPPLY);
  chain.putAta(pk("payout-owner"), tusdMint);
  chain.putCredential(credential, issuer.toBase58(), signers);
  chain.putSchema(schema, schemaOf(credential));
  return { chain, deployment };
}

// ---- tests ----

describe("parseArgs", () => {
  it("parses every step and its flags", () => {
    const owner = pk("payout-owner");
    expect(parseArgs([])).toEqual({ step: "help" });
    expect(parseArgs(["--help"])).toEqual({ step: "help" });
    expect(parseArgs(["keys:check"])).toEqual({ step: "keys:check" });
    expect(parseArgs(["mint"])).toEqual({ step: "mint" });
    expect(parseArgs(["sas"])).toEqual({ step: "sas" });
    expect(parseArgs(["sas", "--signers", `${pk("a")},smoke-gate1=${pk("b")}`])).toEqual({
      step: "sas",
      signers: [{ pubkey: pk("a") }, { pubkey: pk("b"), label: "smoke-gate1" }],
    });
    expect(parseArgs(["sas:signers", "--set", `${pk("a")}, ${pk("b")}`])).toEqual({
      step: "sas:signers",
      set: [{ pubkey: pk("a") }, { pubkey: pk("b") }],
    });
    expect(parseArgs(["config"])).toEqual({ step: "config" });
    expect(parseArgs(["payout", "--owner", owner])).toEqual({ step: "payout", owner });
    expect(parseArgs(["topup", "--to", owner, "--amount", "1200.5"])).toEqual({
      step: "topup",
      to: owner,
      amount: BigInt(1_200_500_000),
    });
    expect(parseArgs(["smoke"])).toEqual({ step: "smoke", mint: "tusd" });
    expect(
      parseArgs(["smoke", "--only", "gate0", "--mint", "circle", "--po", "SMOKE-7KQ2"]),
    ).toEqual({
      step: "smoke",
      only: "gate0",
      mint: "circle",
      po: "SMOKE-7KQ2",
    });
    expect(parseArgs(["alt"])).toEqual({ step: "alt" });
    expect(parseArgs(["verify"])).toEqual({ step: "verify", recording: false });
    expect(parseArgs(["verify", "--recording"])).toEqual({ step: "verify", recording: true });
  });

  it.each([
    [["deploy"], /unknown step "deploy"/],
    [["verify", "--recordings"], /verify has no flag --recordings/],
    [["keys:check", "extra"], /unexpected argument "extra"/],
    [["payout"], /--owner is required/],
    [["payout", "--owner"], /--owner needs a value/],
    [["payout", "--owner", "not-a-key"], /is not a base58 public key/],
    [["topup", "--to", pk("x"), "--amount", "0"], /--amount must be more than 0/],
    [["topup", "--to", pk("x"), "--amount", "1.1234567"], /at most 6 decimals/],
    [["topup", "--to", pk("x"), "--amount", "-5"], /"-5" is not an amount/],
    [["topup", "--to", pk("x")], /--amount is required/],
    [["sas:signers"], /--set is required/],
    [["sas:signers", "--set", `${pk("a")},${pk("a")}`], /is listed twice/],
    [["sas:signers", "--set", `${pk("a")},`], /empty list item/],
    [["sas:signers", "--set", `bad!label=${pk("a")}`], /label "bad!label"/],
    [["smoke", "--only", "gate1"], /--only takes "gate0"/],
    [["smoke", "--mint", "usdt"], /--mint takes "tusd" or "circle"/],
    [["smoke", "--po", "PO-1042-7KQ2"], /--po takes a smoke chain_po_id/],
    [["smoke", "--mint", "tusd", "--mint", "tusd"], /--mint is given twice/],
    [["mint", "--metadata"], /unnamed tUSD mint/],
  ])("refuses %j", (argv, message) => {
    expect(() => parseArgs(argv)).toThrow(UsageError);
    expect(() => parseArgs(argv)).toThrow(message);
  });

  it("formats token amounts back from base units", () => {
    expect(formatTokenAmount(BigInt(100_000_000), 6)).toBe("100");
    expect(formatTokenAmount(BigInt(1_200_500_000), 6)).toBe("1200.5");
    expect(formatTokenAmount(BigInt(5), 6)).toBe("0.000005");
    expect(formatTokenAmount(TUSD_SUPPLY, 6)).toBe("1000000000");
  });
});

describe("devnet refusal", () => {
  it.each([
    "https://api.devnet.solana.com",
    "https://devnet.helius-rpc.com/?api-key=secret",
    "https://solana-devnet.g.alchemy.com/v2/secret",
    "https://rpc.ankr.com/solana_devnet/secret",
  ])("accepts %s", (url) => {
    expect(assertDevnetRpcUrl(url).protocol).toBe("https:");
  });

  it.each([
    ["https://api.mainnet-beta.solana.com", /must be a devnet endpoint/],
    ["https://mainnet.helius-rpc.com/?api-key=devnet", /must be a devnet endpoint/],
    ["https://api.testnet.solana.com", /must be a devnet endpoint/],
    ["https://devnet.example.com/mainnet", /must be a devnet endpoint/],
    ["https://rpc.example.com/?cluster=devnet", /must be a devnet endpoint/],
    ["http://api.devnet.solana.com", /must be https/],
    ["http://127.0.0.1:8899", /must be https/],
    ["devnet", /is not a URL/],
  ])("refuses %s", (url, message) => {
    expect(() => assertDevnetRpcUrl(url)).toThrow(message);
  });

  it("never echoes the URL's query or path secrets", () => {
    const error = (() => {
      try {
        assertDevnetRpcUrl("https://mainnet.helius-rpc.com/v1/path-secret?api-key=query-secret");
      } catch (caught) {
        return caught as Error;
      }
      throw new Error("expected a refusal");
    })();
    expect(error.message).not.toMatch(/secret/);
  });

  it("refuses an endpoint whose genesis hash is not devnet's", () => {
    expect(() => assertDevnetGenesis(DEVNET_GENESIS_HASH)).not.toThrow();
    expect(() => assertDevnetGenesis("5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d")).toThrow(
      /not devnet/,
    );
  });
});

describe("key files", () => {
  let tmp: string;

  beforeAll(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "chain-setup-"));
  });

  afterAll(() => {
    fs.rmSync(tmp, { recursive: true, force: true });
  });

  it("refuses a key path inside the repository, also through .. and a symlink", () => {
    expect(() =>
      assertKeyPathOutsideRepo(path.join(repoRoot, "keys", "admin.json"), repoRoot),
    ).toThrow(/inside the repository/);
    expect(() =>
      assertKeyPathOutsideRepo(path.join(repoRoot, "front", "..", "admin.json"), repoRoot),
    ).toThrow(/inside the repository/);
    const link = path.join(tmp, "repo-link");
    fs.symlinkSync(repoRoot, link);
    expect(() => assertKeyPathOutsideRepo(path.join(link, "admin.json"), repoRoot)).toThrow(
      /inside the repository/,
    );
    expect(() => assertKeyPathOutsideRepo(path.join(tmp, "admin.json"), repoRoot)).not.toThrow();
  });

  it("builds key paths only from a BIDO_KEYS_DIR outside the repository", () => {
    expect(keyFilePath(tmp, "relayer", repoRoot)).toBe(path.join(tmp, "relayer.json"));
    expect(() => keyFilePath(undefined, "relayer", repoRoot)).toThrow(/BIDO_KEYS_DIR is not set/);
    expect(() => keyFilePath("", "relayer", repoRoot)).toThrow(/BIDO_KEYS_DIR is not set/);
    expect(() => keyFilePath(path.join(repoRoot, "keys"), "relayer", repoRoot)).toThrow(
      /inside the repository/,
    );
  });

  it("reads a solana-keygen file and never quotes it in an error", () => {
    const keypair = key("relayer");
    const text = JSON.stringify(Array.from(keypair.secretKey));
    expect(parseKeypairFile(text, "relayer.json").publicKey.toBase58()).toBe(pk("relayer"));
    const forged = Array.from(keypair.secretKey);
    forged[40] ^= 1;
    for (const bad of ["{not json", "[1,2,3]", JSON.stringify(forged)]) {
      const error = (() => {
        try {
          parseKeypairFile(bad, "relayer.json");
        } catch (caught) {
          return caught as Error;
        }
        throw new Error("expected a refusal");
      })();
      expect(error).toBeInstanceOf(SetupError);
      expect(error.message).toMatch(/^relayer\.json /);
      expect(error.message).not.toContain(bad.slice(0, 6));
    }
  });
});

describe("deployments/devnet.json", () => {
  it("round-trips the committed file byte for byte", () => {
    const text = fs.readFileSync(deploymentFile, "utf8");
    const deployment = parseDeployment(text);
    expect(serializeDeployment(deployment)).toBe(text);
  });

  it("holds the fixed public values, and only public keys or blanks elsewhere", () => {
    const deployment = parseDeployment(fs.readFileSync(deploymentFile, "utf8"));
    const program = new PublicKey(PROGRAM_ID);
    // Setup steps fill the other fields on devnet (FD-148); each is blank or a valid public key.
    for (const key of [
      "upgradeAuthority",
      "operator",
      "relayer",
      "tusdTreasuryOwner",
      "sasIssuer",
      "sasCredential",
      "sasSchema",
      "payoutOwner",
    ] as const) {
      const value = deployment[key];
      if (value !== "") expect(() => new PublicKey(value)).not.toThrow();
    }
    expect(deployment).toMatchObject({
      programId: idl.address,
      configPda: configPda(program).toBase58(),
      eventAuthority: eventAuthorityPda(program).toBase58(),
      tusdMint: "J2a18XLaidHWyeStUyEz6Jf11NEYPAQoK3gZ6m9AYnYw",
      sasSoSha256: fs
        .readFileSync(path.join(solanaRoot, "tests/fixtures/sas.so.sha256"), "utf8")
        .split(" ")[0],
      sasSoDumpSlot: 508216959,
    });
    expect(deployment.programId).toBe(PROGRAM_ID);
    expect(deployment.circleDevnetUsdcMint).toBe(CIRCLE_DEVNET_USDC_MINT);
    expect(deployment.sasProgramId).toBe(SAS_PROGRAM_ID);
    expect(deployment.payoutOwnerLabel).toBe(PAYOUT_OWNER_LABEL);
  });

  it("round-trips a finished deployment, including the optional lookup table", async () => {
    const { deployment } = await completeWorld();
    const finished: Deployment = {
      ...deployment,
      measurements: {
        cu: { createPo: 61_000, fund: 18_000, approval: 92_000, release: 29_000, refund: 0 },
        txBytes: { create_po: 1_041, approve_ppap: 829 },
      },
      circleProof: { chainPoId: "SMOKE-7KQ2", escrow: pk("escrow"), signatures: ["5sig"] },
      alt: pk("alt"),
    };
    const text = serializeDeployment(finished);
    expect(parseDeployment(text)).toEqual(finished);
    expect(serializeDeployment(parseDeployment(text))).toBe(text);
    expect(Object.keys(JSON.parse(text))).toEqual([
      "cluster",
      "programId",
      "upgradeAuthority",
      "configPda",
      "eventAuthority",
      "operator",
      "relayer",
      "tusdMint",
      "tusdTreasuryOwner",
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
    ]);
    expect(serializeDeployment(emptyDeployment())).not.toContain('"alt"');
  });

  it("refuses another cluster, unknown fields and malformed values", () => {
    const base = JSON.parse(serializeDeployment(emptyDeployment())) as Record<string, unknown>;
    const text = (patch: Record<string, unknown>) => JSON.stringify({ ...base, ...patch });
    expect(() => parseDeployment(text({ cluster: "mainnet-beta" }))).toThrow(
      /cluster must be "devnet"/,
    );
    expect(() => parseDeployment(text({ privateKey: "x" }))).toThrow(/unknown field "privateKey"/);
    expect(() => parseDeployment(text({ operator: "nope" }))).toThrow(
      /operator is not a base58 address/,
    );
    expect(() => parseDeployment(text({ tusdSupply: 5 }))).toThrow(/tusdSupply must be a string/);
    expect(() => parseDeployment(text({ smokeSigners: ["x"] }))).toThrow(/smokeSigners/);
    expect(() => parseDeployment(text({ sasSignerLabels: { x: "a" } }))).toThrow(
      /sasSignerLabels key/,
    );
    expect(() => parseDeployment(text({ circleProof: { chainPoId: "x" } }))).toThrow(/circleProof/);
    expect(() => parseDeployment("[]")).toThrow(/not a JSON object/);
  });

  it("prints the NEXT_PUBLIC_* lines, unset ones as comments", async () => {
    expect(envLines(emptyDeployment())).toEqual([
      "NEXT_PUBLIC_SOLANA_CLUSTER=devnet",
      "# NEXT_PUBLIC_BIDO_ESCROW_PROGRAM_ID is not set yet (run `config`)",
      "# NEXT_PUBLIC_TUSD_MINT is not set yet (run `mint`)",
      "# NEXT_PUBLIC_SAS_CREDENTIAL is not set yet (run `sas`)",
      "# NEXT_PUBLIC_SAS_SCHEMA is not set yet (run `sas`)",
    ]);
    const { deployment } = await completeWorld();
    expect(envLines({ ...deployment, alt: pk("alt") })).toEqual([
      "NEXT_PUBLIC_SOLANA_CLUSTER=devnet",
      `NEXT_PUBLIC_BIDO_ESCROW_PROGRAM_ID=${PROGRAM_ID}`,
      `NEXT_PUBLIC_TUSD_MINT=${deployment.tusdMint}`,
      `NEXT_PUBLIC_SAS_CREDENTIAL=${deployment.sasCredential}`,
      `NEXT_PUBLIC_SAS_SCHEMA=${deployment.sasSchema}`,
      `NEXT_PUBLIC_BIDO_ALT=${pk("alt")}`,
    ]);
  });
});

describe("mint planner", () => {
  const treasury = pk("treasury");
  const mintAddress = pk("tusd-mint");

  async function planOn(
    mint: Parameters<FakeChain["putMint"]>[1] | null,
    recordedMint = mintAddress,
  ) {
    const chain = new FakeChain();
    if (mint !== null) chain.putMint(mintAddress, mint);
    const account = await chain.getAccount(mintAddress);
    return planMint({
      recordedMint,
      mint: account === null ? null : decodeMint(mintAddress, account, "tUSD mint"),
      treasury,
    });
  }

  it("creates the mint when none is recorded, and does nothing once it is complete", async () => {
    expect(await planOn(null, "")).toEqual([{ kind: "create_mint" }]);
    expect(await planOn({ supply: TUSD_SUPPLY })).toEqual([]);
  });

  it("finishes an interrupted mint while the treasury holds the authority", async () => {
    expect(await planOn({ supply: TUSD_SUPPLY, mintAuthority: treasury })).toEqual([
      { kind: "revoke_mint_authority" },
    ]);
    expect(await planOn({ supply: BigInt(10), mintAuthority: treasury })).toEqual([
      { kind: "mint_remaining", amount: TUSD_SUPPLY - BigInt(10) },
      { kind: "revoke_mint_authority" },
    ]);
  });

  it.each([
    ["the recorded mint is missing", null, /does not exist/],
    ["the decimals differ", { supply: TUSD_SUPPLY, decimals: 9 }, /9 decimals/],
    [
      "a freeze authority is set",
      { supply: TUSD_SUPPLY, freezeAuthority: pk("x") },
      /freeze authority/,
    ],
    ["the supply is too high", { supply: TUSD_SUPPLY + BigInt(1) }, /above/],
    ["the supply is short and revoked", { supply: BigInt(10) }, /mint authority is revoked/],
    [
      "another key holds the authority",
      { supply: TUSD_SUPPLY, mintAuthority: pk("x") },
      /not treasury\.json/,
    ],
  ] as const)("stops when %s", async (_, mint, message) => {
    await expect(planOn(mint)).rejects.toThrow(message);
  });

  it("builds one transaction that ends with the mint authority revoked", () => {
    const instructions = createMintIxs({
      treasury: key("treasury").publicKey,
      mint: key("tusd-mint").publicKey,
      rentLamports: 1_461_600,
    });
    expect(instructions.map((ix) => ix.programId.toBase58())).toEqual([
      "11111111111111111111111111111111",
      TOKEN_PROGRAM_ID.toBase58(),
      "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
      TOKEN_PROGRAM_ID.toBase58(),
      TOKEN_PROGRAM_ID.toBase58(),
    ]);
    // SetAuthority (6), MintTokens (0), no new authority (0).
    expect(Array.from(instructions[4].data)).toEqual([6, 0, 0]);
    // InitializeMint2 (20), 6 decimals, then the mint authority and no freeze authority.
    expect(instructions[1].data[0]).toBe(20);
    expect(instructions[1].data[1]).toBe(6);
    expect(instructions[1].data[34]).toBe(0);
  });
});

describe("sas planner", () => {
  const issuer = key("sas-issuer").publicKey;
  const credential = credentialPda(issuer, SAS_CREDENTIAL_NAME).toBase58();
  const schema = schemaPda(
    new PublicKey(credential),
    SAS_SCHEMA_NAME,
    SAS_SCHEMA_VERSION,
  ).toBase58();

  async function planOn(chain: FakeChain) {
    const [c, s] = [await chain.getAccount(credential), await chain.getAccount(schema)];
    return planSas({
      issuer: issuer.toBase58(),
      credentialAddress: credential,
      credential: c === null ? null : decodeCredential(c),
      schema: s === null ? null : decodeSchema(s),
    });
  }

  it("creates what is missing and nothing once both match", async () => {
    const chain = new FakeChain();
    expect(await planOn(chain)).toEqual([{ kind: "create_credential" }, { kind: "create_schema" }]);
    chain.putCredential(credential, issuer.toBase58(), [pk("smoke-gate0")]);
    expect(await planOn(chain)).toEqual([{ kind: "create_schema" }]);
    chain.putSchema(schema, schemaOf(credential));
    expect(await planOn(chain)).toEqual([]);
  });

  it("stops with the fix when the existing schema differs", async () => {
    const chain = new FakeChain();
    chain.putCredential(credential, issuer.toBase58(), [pk("smoke-gate0")]);
    chain.putSchema(schema, { ...schemaOf(credential), layout: [13, 0, 12], isPaused: true });
    await expect(planOn(chain)).rejects.toThrow(
      /layout is \[13,0,12\].*isPaused.*ChangeSchemaVersion/,
    );
  });

  it("stops when the credential belongs to another authority", async () => {
    const chain = new FakeChain();
    chain.putCredential(credential, pk("someone"), []);
    await expect(planOn(chain)).rejects.toThrow(/wanted sas-issuer\.json/);
  });

  it("builds CreateCredential, CreateSchema and ChangeAuthorizedSigners for SAS", () => {
    const create = createCredentialIx(issuer, [pk("smoke-gate0"), pk("smoke-gate1")]);
    expect(create.programId.toBase58()).toBe(SAS_PROGRAM_ID);
    expect(create.data[0]).toBe(0);
    expect(create.keys.map((k) => [k.pubkey.toBase58(), k.isSigner, k.isWritable])).toEqual([
      [issuer.toBase58(), true, true],
      [credential, false, true],
      [issuer.toBase58(), true, false],
      ["11111111111111111111111111111111", false, false],
    ]);
    const schemaIx = createSchemaIx(issuer, new PublicKey(credential));
    expect(schemaIx.data[0]).toBe(1);
    expect(schemaIx.keys.map((k) => k.pubkey.toBase58())).toContain(schema);
    expect(Buffer.from(schemaIx.data).includes(Buffer.from(SAS_SCHEMA_DESCRIPTION))).toBe(true);
    const change = changeAuthorizedSignersIx(issuer, new PublicKey(credential), [pk("sqe")]);
    expect(change.data[0]).toBe(3);
    expect(Buffer.from(change.data).subarray(-32).equals(key("sqe").publicKey.toBuffer())).toBe(
      true,
    );
  });
});

describe("signers", () => {
  it("changes only when the set differs", () => {
    expect(signersNeedChange([pk("a"), pk("b")], [pk("b"), pk("a")])).toBe(false);
    expect(signersNeedChange([pk("a"), pk("b")], [pk("a")])).toBe(true);
    expect(signersNeedChange([pk("a")], [pk("a"), pk("c")])).toBe(true);
    expect(signersNeedChange([pk("a"), pk("b")], [pk("a"), pk("c")])).toBe(true);
  });

  it("labels given, then known, then plain signers", () => {
    expect(
      signerLabels([{ pubkey: pk("a"), label: "SQE" }, { pubkey: pk("b") }, { pubkey: pk("c") }], {
        [pk("b")]: "smoke-gate1",
        [pk("z")]: "gone",
      }),
    ).toEqual({ [pk("a")]: "SQE", [pk("b")]: "smoke-gate1", [pk("c")]: "signer" });
  });

  it("requires the non-smoke signers at recording", async () => {
    const { deployment } = await completeWorld();
    expect(recordingSigners(deployment)).toEqual([pk("buyer-approver"), pk("sqe")]);
  });
});

describe("config planner", () => {
  const wanted: WantedConfig = {
    admin: pk("admin"),
    operator: pk("operator"),
    allowedMints: [pk("tusd-mint"), CIRCLE_DEVNET_USDC_MINT],
    sasCredential: pk("credential"),
    sasSchema: pk("schema"),
  };
  const current: ConfigAccount = { ...wanted, bump: 254 };
  const none = { operator: null, allowedMints: null, sasCredential: null, sasSchema: null };

  it("initialises a missing Config and leaves a matching one alone", async () => {
    expect(planConfig(wanted, null)).toEqual({ kind: "init" });
    expect(planConfig(wanted, current)).toEqual({ kind: "none" });
    const chain = new FakeChain();
    await chain.putConfig(current);
    const account = await chain.getAccount(configPda(new PublicKey(PROGRAM_ID)).toBase58());
    const { decodeConfig } = await import("../src/client/escrow");
    expect(planConfig(wanted, decodeConfig(account?.data ?? new Uint8Array()))).toEqual({
      kind: "none",
    });
  });

  it("updates only the differing fields", () => {
    expect(planConfig(wanted, { ...current, operator: pk("old-operator") })).toEqual({
      kind: "update",
      args: { ...none, operator: pk("operator") },
    });
    expect(
      planConfig(wanted, {
        ...current,
        allowedMints: [pk("tusd-mint"), PublicKey.default.toBase58()],
      }),
    ).toEqual({ kind: "update", args: { ...none, allowedMints: wanted.allowedMints } });
    expect(planConfig(wanted, { ...current, sasSchema: pk("old-schema") })).toEqual({
      kind: "update",
      args: { ...none, sasSchema: pk("schema") },
    });
  });

  it("syncs admin after an upgrade-authority rotation with an all-null update", () => {
    expect(planConfig(wanted, { ...current, admin: pk("old-admin") })).toEqual({
      kind: "update",
      args: none,
    });
  });

  it("builds init_config and update_config with the IDL's accounts and arguments", () => {
    const program = new PublicKey(PROGRAM_ID);
    const common = {
      programId: program,
      admin: key("admin").publicKey,
      credential: new PublicKey(pk("credential")),
      schema: new PublicKey(pk("schema")),
    };
    const init = initConfigIx({
      ...common,
      operator: key("operator").publicKey,
      allowedMints: [new PublicKey(pk("tusd-mint")), new PublicKey(CIRCLE_DEVNET_USDC_MINT)],
    });
    const update = updateConfigIx({ ...common, args: { ...none, operator: pk("operator") } });
    for (const [name, ix] of [
      ["init_config", init],
      ["update_config", update],
    ] as const) {
      const spec = idl.instructions.find((i) => i.name === name);
      expect(spec).toBeDefined();
      expect(ix.keys.map((k) => [k.isSigner, k.isWritable])).toEqual(
        (spec?.accounts ?? []).map((a) => [
          "signer" in a && a.signer === true,
          "writable" in a && a.writable === true,
        ]),
      );
      for (const [i, account] of (spec?.accounts ?? []).entries()) {
        if ("address" in account && account.address !== undefined) {
          expect(ix.keys[i].pubkey.toBase58()).toBe(account.address);
        }
      }
      expect(escrowCoder.instruction.decode(Buffer.from(ix.data))?.name).toBe(name);
    }
    expect(init.keys[1].pubkey.toBase58()).toBe(configPda(program).toBase58());
    expect(init.keys[3].pubkey.toBase58()).toBe(programDataAddress(PROGRAM_ID));
    const decoded = escrowCoder.instruction.decode(Buffer.from(update.data));
    const args = (decoded?.data as { args: Record<string, PublicKey | null> }).args;
    expect(args.operator?.toBase58()).toBe(pk("operator"));
    expect([args.allowed_mints, args.sas_credential, args.sas_schema]).toEqual([null, null, null]);
  });
});

describe("payout and top-up planners", () => {
  const owner = pk("payout-owner");
  const mint = pk("tusd-mint");
  const ataAddress = getAssociatedTokenAddressSync(
    new PublicKey(mint),
    new PublicKey(owner),
    true,
  ).toBase58();

  async function planOn(chain: FakeChain) {
    const account = await chain.getAccount(ataAddress);
    return planPayout({
      ataAddress,
      ata: account === null ? null : decodeTokenAccount(ataAddress, account, "payout"),
      owner,
      mint,
    });
  }

  it("creates the payout account once", async () => {
    const chain = new FakeChain();
    expect(await planOn(chain)).toBe(true);
    chain.putAta(owner, mint);
    expect(await planOn(chain)).toBe(false);
  });

  it("stops on a foreign account at the payout address", async () => {
    const chain = new FakeChain();
    chain.putTokenAccount(ataAddress, { mint, owner: pk("someone"), amount: BigInt(0) });
    await expect(planOn(chain)).rejects.toThrow(/is not the tUSD account of/);
  });

  it("tops up only the shortfall", () => {
    expect(planTopUp(BigInt(0), BigInt(200_000_000))).toBe(BigInt(200_000_000));
    expect(planTopUp(BigInt(50_000_000), BigInt(200_000_000))).toBe(BigInt(150_000_000));
    expect(planTopUp(BigInt(200_000_000), BigInt(200_000_000))).toBe(BigInt(0));
    expect(planTopUp(BigInt(300_000_000), BigInt(200_000_000))).toBe(BigInt(0));
  });
});

describe("smoke planner", () => {
  const program = new PublicKey(PROGRAM_ID);

  function escrow(status: PoEscrowAccount["status"], released: number[] = []): PoEscrowAccount {
    const { args } = smokePoArgs({
      chainPoId: "SMOKE-7KQ2",
      total: BigInt(100_000_000),
      gateAttesters: [key("smoke-gate0").publicKey, key("smoke-gate1").publicKey],
      nowSeconds: 1_791_000_000,
    });
    return {
      chainPoId: args.chainPoId,
      buyer: pk("smoke-buyer"),
      payout: pk("payout-owner"),
      payoutToken: pk("payout-token"),
      mint: pk("tusd-mint"),
      vault: pk("vault"),
      poHash: String(args.poHash),
      cadHash: String(args.cadHash),
      total: args.total,
      released: BigInt(0),
      status,
      credential: pk("credential"),
      schema: pk("schema"),
      gates: args.gateAttesters.map((attester) => ({
        attester: attester.toBase58(),
        attestation: pk("a"),
      })),
      milestones: args.milestones.map((m, i) => ({
        ...m,
        amount: BigInt(0),
        status: released.includes(i) ? ("Released" as const) : ("Pending" as const),
        releasedSlot: BigInt(0),
        approvedBy: PublicKey.default.toBase58(),
        evidenceHash: "00".repeat(32),
      })),
      refundAfter: args.refundAfter,
      bump: 254,
    };
  }

  it("plans the remaining steps of one escrow from its state", () => {
    expect(planSmoke(null)).toEqual(["create_po", "fund", "approve_gate0", "approve_gate1"]);
    expect(planSmoke(escrow("Created"))).toEqual(["fund", "approve_gate0", "approve_gate1"]);
    expect(planSmoke(escrow("Funded"))).toEqual(["approve_gate0", "approve_gate1"]);
    expect(planSmoke(escrow("Funded", [0]))).toEqual(["approve_gate1"]);
    expect(planSmoke(escrow("Completed", [0, 1, 2]))).toEqual([]);
    expect(() => planSmoke(escrow("Refunded"))).toThrow(/is Refunded/);
  });

  it("stops after gate 0 for --only gate0 and after funding for the failed-release escrow", () => {
    expect(planSmoke(null, "gate0")).toEqual(["create_po", "fund", "approve_gate0"]);
    expect(planSmoke(escrow("Funded", [0]), "gate0")).toEqual([]);
    expect(planSmoke(null, "fund")).toEqual(["create_po", "fund"]);
    expect(planSmoke(escrow("Funded"), "fund")).toEqual([]);
  });

  it("derives valid create_po arguments for a smoke PO", () => {
    const { args, payoutRefHex } = smokePoArgs({
      chainPoId: "SMOKE-7KQ2",
      total: BigInt(100_000_000),
      gateAttesters: [key("smoke-gate0").publicKey, key("smoke-gate1").publicKey],
      nowSeconds: 1_791_000_000.7,
    });
    expect(CHAIN_PO_ID_RE.test(args.chainPoId)).toBe(true);
    expect(args.milestones.map((m) => m.bps).reduce((a, b) => a + b, 0)).toBe(10_000);
    expect(args.milestones.map((m) => m.gate)).toEqual([0, 1, 1]);
    expect(args.refundAfter).toBe(BigInt(1_791_000_000 + 7_776_000));
    expect(payoutRefHex).toMatch(/^[0-9a-f]{64}$/);
    expect(args.poHash).not.toBe(args.cadHash);
    expect(escrowPda(program, key("smoke-buyer").publicKey, args.chainPoId)[0]).toBeInstanceOf(
      PublicKey,
    );
  });

  it("keeps the maximum of every measurement", () => {
    const current: Measurements = {
      cu: { createPo: 60_000, fund: 20_000, approval: 0, release: 30_000, refund: 0 },
      txBytes: { create_po: 1_000, approve_ppap: 0 },
    };
    expect(
      mergeMeasurements(current, {
        cu: { createPo: 50_000, approval: 90_000 },
        txBytes: { approve_ppap: 830 },
      }),
    ).toEqual({
      cu: { createPo: 60_000, fund: 20_000, approval: 90_000, release: 30_000, refund: 0 },
      txBytes: { create_po: 1_000, approve_ppap: 830 },
    });
  });

  it("reads per-release compute units from the logs and the failed release's error code", () => {
    const logs = [
      "Program ComputeBudget111111111111111111111111111111 invoke [1]",
      "Program ComputeBudget111111111111111111111111111111 success",
      `Program ${SAS_PROGRAM_ID} invoke [1]`,
      `Program ${SAS_PROGRAM_ID} consumed 21000 of 199850 compute units`,
      `Program ${SAS_PROGRAM_ID} success`,
      `Program ${PROGRAM_ID} invoke [1]`,
      "Program log: Instruction: Release",
      `Program ${TOKEN_PROGRAM_ID.toBase58()} invoke [2]`,
      `Program ${TOKEN_PROGRAM_ID.toBase58()} consumed 6200 of 150000 compute units`,
      `Program ${TOKEN_PROGRAM_ID.toBase58()} success`,
      `Program ${PROGRAM_ID} invoke [2]`,
      `Program ${PROGRAM_ID} consumed 2000 of 140000 compute units`,
      `Program ${PROGRAM_ID} success`,
      `Program ${PROGRAM_ID} consumed 31000 of 178850 compute units`,
      `Program ${PROGRAM_ID} success`,
      `Program ${PROGRAM_ID} invoke [1]`,
      `Program ${PROGRAM_ID} consumed 28500 of 147850 compute units`,
      `Program ${PROGRAM_ID} success`,
    ];
    expect(topLevelUnits(logs, PROGRAM_ID)).toEqual([31_000, 28_500]);
    expect(customErrorCode({ InstructionError: [2, { Custom: 6026 }] })).toBe(6026);
    expect(customErrorCode({ InstructionError: [0, "InvalidAccountData"] })).toBeNull();
    expect(customErrorCode(null)).toBeNull();
  });

  it("prefixes escrow transactions with the §3.9 compute-budget pair from CU_LIMITS", () => {
    const [limit, price] = computeBudgetIxs(cuLimitFor("createPo"));
    expect(limit.programId.toBase58()).toBe("ComputeBudget111111111111111111111111111111");
    expect(limit.data[0]).toBe(2);
    expect(Buffer.from(limit.data).readUInt32LE(1)).toBe(CU_LIMITS.createPo);
    expect(price.data[0]).toBe(3);
    expect(cuLimitFor("release", 2)).toBe(2 * CU_LIMITS.release);
    expect(cuLimitFor("approval")).toBe(CU_LIMITS.approval);
  });
});

describe("alt planner", () => {
  const measured = (create_po: number, approve_ppap: number): Measurements => ({
    ...emptyDeployment().measurements,
    txBytes: { create_po, approve_ppap },
  });

  it("creates a table only when a measured transaction is too large", async () => {
    const { deployment } = await completeWorld();
    const wanted = altAddresses(deployment);
    expect(wanted).toHaveLength(8);
    expect(planAlt({ measurements: measured(0, 0), wanted, existing: null })).toEqual({
      kind: "unmeasured",
    });
    expect(planAlt({ measurements: measured(1_050, 835), wanted, existing: null })).toEqual({
      kind: "not_needed",
    });
    expect(
      planAlt({ measurements: measured(MAX_TX_BYTES + 1, 835), wanted, existing: null }),
    ).toEqual({
      kind: "create",
      addresses: wanted,
    });
  });

  it("extends an existing table with what it lacks, then does nothing", async () => {
    const { deployment } = await completeWorld();
    const wanted = altAddresses(deployment);
    const chain = new FakeChain();
    chain.putLookupTable(pk("alt"), pk("deploy-payer"), wanted.slice(0, 3));
    const account = await chain.getAccount(pk("alt"));
    const existing = decodeLookupTable(pk("alt"), account as AccountSnapshot);
    expect(existing).toEqual(wanted.slice(0, 3));
    expect(planAlt({ measurements: measured(0, 0), wanted, existing })).toEqual({
      kind: "extend",
      addresses: wanted.slice(3),
    });
    expect(planAlt({ measurements: measured(0, 0), wanted, existing: wanted })).toEqual({
      kind: "none",
    });
  });
});

describe("verify", () => {
  it("passes on a finished setup, with and without --recording rules that hold", async () => {
    const { chain, deployment } = await completeWorld();
    expect(await verifyDeployment(deployment, chain, { recording: false })).toEqual([]);
    const issuer = deployment.sasIssuer;
    chain.putCredential(deployment.sasCredential, issuer, [pk("buyer-approver"), pk("sqe")]);
    const frozen = {
      ...deployment,
      sasSignerLabels: { [pk("buyer-approver")]: "buyer approver", [pk("sqe")]: "SQE" },
    };
    expect(await verifyDeployment(frozen, chain, { recording: true })).toEqual([]);
  });

  it("lists every field that setup has not recorded yet", async () => {
    const blank: Deployment = {
      ...emptyDeployment(),
      programId: PROGRAM_ID,
      tusdMint: "J2a18XLaidHWyeStUyEz6Jf11NEYPAQoK3gZ6m9AYnYw",
    };
    const problems = await verifyDeployment(blank, new FakeChain(), { recording: false });
    expect(problems).toEqual(
      expect.arrayContaining([
        "upgradeAuthority is not recorded (run `config`)",
        "sasCredential is not recorded (run `sas`)",
        "payoutOwner is not recorded (run `payout`)",
        `program ${PROGRAM_ID} is not deployed`,
      ]),
    );
  });

  it("reports each mismatch with the chain", async () => {
    const { chain, deployment } = await completeWorld();
    chain.putMint(deployment.tusdMint, {
      supply: TUSD_SUPPLY - BigInt(1),
      mintAuthority: pk("treasury"),
    });
    await chain.putConfig({
      admin: pk("admin"),
      operator: pk("someone"),
      allowedMints: [deployment.tusdMint, PublicKey.default.toBase58()],
      sasCredential: deployment.sasCredential,
      sasSchema: deployment.sasSchema,
      bump: 255,
    });
    chain.accounts.delete(
      payoutAta(
        new PublicKey(deployment.payoutOwner),
        new PublicKey(deployment.tusdMint),
      ).toBase58(),
    );
    chain.putSchema(deployment.sasSchema, {
      ...schemaOf(deployment.sasCredential),
      description: "other",
    });
    chain.putProgram(pk("someone"), ELF);
    chain.genesis = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d";
    const problems = await verifyDeployment(deployment, chain, { recording: false });
    expect(problems).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/not devnet/),
        expect.stringMatching(/upgrade authority is .* not /),
        expect.stringMatching(/^Config\.operator is /),
        expect.stringMatching(/^Config\.allowedMints is /),
        expect.stringMatching(/^tUSD supply is /),
        expect.stringMatching(/^tUSD mint authority is /),
        expect.stringMatching(/^the payout tUSD account .* does not exist$/),
        expect.stringMatching(/^schema description is "other"/),
      ]),
    );
  });

  it("--recording refuses smoke signers and a program hash that is not the published build", async () => {
    const { chain, deployment } = await completeWorld();
    const problems = await verifyDeployment(
      { ...deployment, build: { ...deployment.build, executableHash: "00".repeat(32) } },
      chain,
      { recording: true },
    );
    expect(problems).toEqual([
      expect.stringMatching(
        /^--recording: credential signers must be exactly the buyer approver and the SQE/,
      ),
      expect.stringMatching(/^--recording: the program hash is /),
    ]);
  });

  it("hashes the deployed program as solana-verify does, without the zero padding", () => {
    const padded = new Uint8Array([...ELF, 0, 0, 0, 0]);
    expect(programHash(padded)).toBe(programHash(ELF.subarray(0, ELF.length)));
    expect(programHash(ELF)).toMatch(/^[0-9a-f]{64}$/);
  });
});
