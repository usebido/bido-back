# Bido Solana escrow

Standalone on-chain workspace for the Bido payment MVP, on **Solana devnet only**.
Imported from `bido-agent-procurements` commit
`ac3faee24734d8584a700b81529f81bee33d6e08`. The Python API, payment integration,
relayer and passkey service are not part of this workspace.

The `bido_escrow` Anchor program reserves a purchase order's tokens and releases
them to its fixed payout account by milestones, verified against Solana
Attestation Service (SAS) attestations. It supports configuration, purchase order
creation, funding, release and refund.

## Layout

- `programs/bido_escrow/`: Rust program and LiteSVM integration tests.
- `idl/`: committed Anchor IDL and generated TypeScript type.
- `tests/fixtures/`: SAS program binary, its checksum, golden vectors and escrow account bytes.
- `src/client/`: TypeScript instruction builders, account decoders, hashes and PDAs used by setup.
- `src/contracts/`: chain constants and error types extracted from the original contracts.
- `scripts/`: IDL synchronization and devnet setup, with setup tests.
- `deployments/devnet.json`: imported public deployment record; not a fresh network verification.
- `THIRD_PARTY_NOTICES`: provenance of the SAS fixture.

## Build and check

Toolchain: Anchor **1.2.0**, Agave **4.1.2**, Rust **1.97.1 or newer**,
Node **24 or newer**, pnpm **11.21.0**. Versions and Rust dependencies are
preserved in `Anchor.toml`, `Cargo.toml` and `Cargo.lock`.

From the backend repository root:

```sh
pnpm --dir solana install --frozen-lockfile
bash solana/scripts/check.sh
```

The check builds the program, runs the Rust and TypeScript suites, checks Rust
formatting, verifies the SAS fixture checksum and checks IDL drift. Tests run
locally with LiteSVM or fake chain readers; they do not use a devnet RPC or real keys.

Anchor may warn that its generated local build keypair differs from the fixed
program ID. Do not run `anchor keys sync`: the deployed program ID must stay
`7992GKVq51ChvsVJQ9GFto2aBozUGuMcuY93SUSxexJw`.

After changing the program, regenerate the committed IDL:

```sh
(cd solana && anchor build && node scripts/sync-idl.mjs)
```

`sync-idl.mjs --check` compares both committed files byte for byte with the build
and checks the program ID against Rust, Anchor and the deployment record.
Consumers can copy the JSON from `solana/idl/bido_escrow.json`.

The Rust vector tests read `tests/fixtures/vectors.json`; set `UPDATE_VECTORS=1`
when deliberately regenerating vectors. Preserve every section of that file.

## Devnet operations

View the available commands without accessing keys or RPC:

```sh
pnpm --dir solana setup:devnet --help
```

The setup CLI retains the original `keys:check`, `mint`, `sas`, `sas:signers`,
`config`, `payout`, `topup`, `smoke`, `alt` and `verify` steps. Real devnet
operations and deployments remain manual. Set `SOLANA_RPC_URL` to a devnet RPC
and `BIDO_KEYS_DIR` to a directory **outside the entire backend repository**.
The CLI also supports a local, ignored `solana/.env.local`. Never commit keys.

The setup CLI writes public addresses and measurements to
`solana/deployments/devnet.json`. The Python service does not automatically
consume this record or the IDL. Its Docker build excludes this workspace.
