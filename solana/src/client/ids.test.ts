// @vitest-environment node
import { describe, expect, it } from "vitest";

import { CHAIN_PO_ID_RE, MAX_CHAIN_PO_ID_LEN } from "../contracts";

import { isChainPoId, newChainPoId } from "./ids";

const CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const byteLength = (text: string) => new TextEncoder().encode(text).length;

describe("newChainPoId", () => {
  it("appends a dash and 4 Crockford base32 characters, matching CHAIN_PO_ID_RE", () => {
    const id = newChainPoId("PO-1042");
    expect(id).toMatch(/^PO-1042-[0-9A-HJKMNP-TV-Z]{4}$/);
    expect(CHAIN_PO_ID_RE.test(id)).toBe(true);
    expect(byteLength(id)).toBeLessThanOrEqual(MAX_CHAIN_PO_ID_LEN);
  });

  it("fits the longest PO number in exactly 32 bytes", () => {
    const id = newChainPoId("PO-".padEnd(27, "9"));
    expect(CHAIN_PO_ID_RE.test(id)).toBe(true);
    expect(byteLength(id)).toBe(MAX_CHAIN_PO_ID_LEN);
  });

  it("draws a fresh suffix from the whole alphabet", () => {
    const ids = Array.from({ length: 2_000 }, () => newChainPoId("CTRL-1042"));
    expect(new Set(ids).size).toBeGreaterThan(1_900);
    const seen = new Set(ids.flatMap((id) => [...id.slice(-4)]));
    expect([...seen].sort().join("")).toBe(CROCKFORD_BASE32);
  });

  it("rejects a PO number that cannot make a valid chain_po_id", () => {
    for (const bad of [
      "",
      "po-1042",
      "PO 1042",
      "PO_1042",
      "PO-1042\n",
      "ПО-1042",
      "A".repeat(28),
    ]) {
      expect(() => newChainPoId(bad), JSON.stringify(bad)).toThrow(RangeError);
    }
  });
});

describe("isChainPoId (PO-AC-14)", () => {
  it("accepts a well-formed chain_po_id", () => {
    expect(isChainPoId("PO-1042-7KQ2")).toBe(true);
    expect(isChainPoId(newChainPoId("PO-1042"))).toBe(true);
  });

  it("accepts the longest id, exactly 32 bytes", () => {
    const id = `PO-${"1".repeat(24)}-7KQ2`;
    expect(byteLength(id)).toBe(MAX_CHAIN_PO_ID_LEN);
    expect(isChainPoId(id)).toBe(true);
  });

  it("rejects a 33-byte id", () => {
    const id = `PO-${"1".repeat(25)}-7KQ2`;
    expect(byteLength(id)).toBe(MAX_CHAIN_PO_ID_LEN + 1);
    expect(isChainPoId(id)).toBe(false);
  });

  it("rejects ids outside CHAIN_PO_ID_RE", () => {
    for (const bad of [
      "po-1042",
      "PO-1042-7KQI",
      "PO-1042-7KQL",
      "PO-1042-7KQO",
      "PO-1042-7KQU",
      "PO-1042-7kq2",
      "PO-1042-7KQ",
      "PO-1042-7KQ22",
      "-7KQ2",
      "",
      "PO-1042-7KQ2\n",
      " PO-1042-7KQ2",
      "PO 1042-7KQ2",
      "PO_1042-7KQ2",
      "ПО-1042-7KQ2",
      "PO-1042-7KQ2/../x",
    ]) {
      expect(isChainPoId(bad), JSON.stringify(bad)).toBe(false);
    }
  });
});
