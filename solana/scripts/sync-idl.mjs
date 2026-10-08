#!/usr/bin/env node
// Copies the Anchor build's IDL and TypeScript type into idl/ without modification.
// Run from any directory: node solana/scripts/sync-idl.mjs [--check].
// Also checks the program id against Rust, Anchor.toml and deployments/devnet.json.

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PROGRAM = "bido_escrow";
const BUILD_IDL = `target/idl/${PROGRAM}.json`;
const BUILD_TYPES = `target/types/${PROGRAM}.ts`;

const COPIES = [
  { from: BUILD_IDL, to: `idl/${PROGRAM}.json` },
  { from: BUILD_TYPES, to: `idl/${PROGRAM}.ts` },
];

const LIB_RS = `programs/${PROGRAM}/src/lib.rs`;
const ANCHOR_TOML = "Anchor.toml";
const DEPLOYMENT = "deployments/devnet.json";

/** Bytes of a repository file, or null when it does not exist. */
function read(rel) {
  const abs = path.join(ROOT, rel);
  return existsSync(abs) ? readFileSync(abs) : null;
}

/** `<name> = "<id>"` inside the `[programs.<cluster>]` table of Anchor.toml. */
function anchorTomlProgramId(text, cluster) {
  let table = null;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/#.*$/, "").trim();
    const header = /^\[([^\]]+)\]$/.exec(line);
    if (header) {
      table = header[1].trim();
      continue;
    }
    const entry = /^([A-Za-z0-9_-]+)\s*=\s*"([^"]*)"$/.exec(line);
    if (table === `programs.${cluster}` && entry && entry[1] === PROGRAM) return entry[2];
  }
  return null;
}

/** Every place that names the program id, as `[where, id | null]`. */
function programIds(buildIdl) {
  const libRs = read(LIB_RS);
  const anchorToml = read(ANCHOR_TOML);
  const ids = [
    [
      `${LIB_RS} declare_id!`,
      libRs && (/declare_id!\(\s*"([^"]*)"\s*\)/.exec(libRs.toString("utf8"))?.[1] ?? null),
    ],
    [`${BUILD_IDL} address`, JSON.parse(buildIdl.toString("utf8")).address ?? null],
    [
      `${ANCHOR_TOML} [programs.devnet]`,
      anchorToml && anchorTomlProgramId(anchorToml.toString("utf8"), "devnet"),
    ],
  ];
  const deployment = read(DEPLOYMENT);
  if (deployment) {
    ids.push([
      `${DEPLOYMENT} programId`,
      JSON.parse(deployment.toString("utf8")).programId ?? null,
    ]);
  }
  return ids;
}

function programIdProblems(buildIdl) {
  const ids = programIds(buildIdl);
  const problems = ids.filter(([, id]) => !id).map(([where]) => `${where}: no program id found`);
  const distinct = new Set(ids.map(([, id]) => id).filter(Boolean));
  if (distinct.size > 1) {
    problems.push(
      `program id differs:\n${ids.map(([where, id]) => `  ${where} = ${id ?? "(none)"}`).join("\n")}`,
    );
  }
  return { problems, id: ids[0][1] };
}

function fail(problems) {
  for (const problem of problems) console.error(`sync-idl: ${problem}`);
  process.exit(1);
}

function main() {
  const args = process.argv.slice(2);
  if (args.some((arg) => arg !== "--check")) {
    console.error("usage: node scripts/sync-idl.mjs [--check]");
    process.exit(2);
  }
  const check = args.includes("--check");

  const build = new Map([BUILD_IDL, BUILD_TYPES].map((rel) => [rel, read(rel)]));
  const missing = [...build].filter(([, bytes]) => !bytes).map(([rel]) => rel);
  if (missing.length > 0) {
    fail(missing.map((rel) => `${rel} is missing: run \`anchor build\` first`));
  }

  const { problems, id } = programIdProblems(build.get(BUILD_IDL));
  if (problems.length > 0) fail(problems);

  if (check) {
    const drift = COPIES.filter(({ from, to }) => {
      const copy = read(to);
      return !copy || !copy.equals(build.get(from));
    }).map(({ from, to }) => `${to} differs from ${from}`);
    if (drift.length > 0) {
      fail([...drift, "run `anchor build && node scripts/sync-idl.mjs` and commit the copies"]);
    }
    console.log(`sync-idl: ${COPIES.length} copies equal the build; program id ${id}`);
    return;
  }

  for (const { from, to } of COPIES) {
    const abs = path.join(ROOT, to);
    mkdirSync(path.dirname(abs), { recursive: true });
    writeFileSync(abs, build.get(from));
    console.log(`sync-idl: ${from} -> ${to}`);
  }
}

main();
