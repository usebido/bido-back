#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
shasum -a 256 -c tests/fixtures/sas.so.sha256
anchor build
cargo test --locked -p bido_escrow
cargo fmt --all -- --check
node scripts/sync-idl.mjs --check
pnpm check
