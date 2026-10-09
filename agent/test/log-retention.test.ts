import { describe, expect, it } from "vitest";
import {
  LOG_KEEP_ENTRIES,
  LOG_READ_MAX_BYTES,
  LOG_READ_MAX_LINES,
  byteLength,
  trimLog,
} from "../src/log-retention";
import { goodAsk, call, logFor } from "./helpers";

/**
 * `/workspace/log.md` is bounded, and a trim says so. straightedge#131.
 *
 * Two layers, deliberately. `trimLog` is pure and gets the edge cases; then
 * the LAST describe drives real asks through the shipped Worker and reads the
 * file back out of the Durable Object's real SQLite, because a suite of pure
 * calls proves the decision path and never the shipped artifact. Only the
 * network is replaced in this project's agent tests (see vitest.config.ts).
 *
 * The numbers are not asserted as literals. Every expectation is written
 * against `LOG_KEEP_ENTRIES`, `LOG_READ_MAX_BYTES` and `LOG_READ_MAX_LINES`,
 * so a deliberate change to the bound moves the tests with it and an
 * ACCIDENTAL one still reds `the bound is the desk's own window` below, which
 * is the single place the derivation is pinned.
 */

function entry(role: "user" | "assistant", n: number, body = "x"): string {
  return `## 2026-10-08T00:00:${String(n % 60).padStart(2, "0")}.000Z ${role}\n\n${body}\n\n`;
}

function logOf(count: number, body = "x"): string {
  let out = "";
  for (let i = 0; i < count; i += 1) {
    out += entry(i % 2 === 0 ? "user" : "assistant", i, body);
  }
  return out;
}

function countEntries(log: string): number {
  return (log.match(/^## /gm) ?? []).length;
}

describe("the bound is derived, not chosen", () => {
  it("the bound is the desk's own window", () => {
    // src/straightedge/llm.py: KEEP_TURNS = 40, "Bound to KEEP_TURNS
    // messages", one role's message per unit. An entry here is also one
    // role's message. If this ever disagrees, one of the two moved alone.
    expect(LOG_KEEP_ENTRIES).toBe(40);
  });

  it("the byte and line caps are the read tool's own limits", () => {
    // Held at the read limits so `read` with no offset returns the WHOLE log.
    // Otherwise the model's unprompted read returns the file's FIRST lines,
    // which on a long log is its OLDEST turns.
    expect(LOG_READ_MAX_BYTES).toBe(32 * 1024);
    expect(LOG_READ_MAX_LINES).toBe(800);
  });
});

describe("trimLog: the entry window", () => {
  it("leaves a short log completely alone, marker included", () => {
    const log = logOf(4);
    const out = trimLog(log);
    expect(out.text).toBe(log);
    expect(out.droppedEntries).toBe(0);
    expect(out.text).not.toContain("straightedge log trimmed");
  });

  it("keeps exactly the window when the log is exactly at it", () => {
    const out = trimLog(logOf(LOG_KEEP_ENTRIES));
    expect(out.droppedEntries).toBe(0);
    expect(countEntries(out.text)).toBe(LOG_KEEP_ENTRIES);
  });

  it("drops the OLDEST entries and keeps the newest", () => {
    const out = trimLog(logOf(LOG_KEEP_ENTRIES + 5));
    expect(out.droppedEntries).toBe(5);
    expect(countEntries(out.text)).toBe(LOG_KEEP_ENTRIES);
    // Entry 0 is gone; the last one written is still there.
    expect(out.text).not.toContain("00:00:00.000Z user");
    expect(out.text).toContain(`00:00:${String((LOG_KEEP_ENTRIES + 4) % 60).padStart(2, "0")}.000Z`);
  });

  it("reports the drop in the file itself, never silently", () => {
    const out = trimLog(logOf(LOG_KEEP_ENTRIES + 3));
    expect(out.text.startsWith("<!-- straightedge log trimmed: 3 earlier entries (")).toBe(true);
    expect(out.text).toContain("straightedge#131");
  });

  it("accumulates the totals across repeated trims", () => {
    let log = logOf(LOG_KEEP_ENTRIES);
    let total = 0;
    for (let round = 0; round < 5; round += 1) {
      log += entry("user", 50 + round);
      const out = trimLog(log);
      total += out.droppedEntries;
      expect(out.totalDroppedEntries).toBe(total);
      log = out.text;
    }
    expect(total).toBe(5);
    expect(log).toContain("5 earlier entries");
    // The marker is replaced, never stacked, and is never itself counted as
    // an entry.
    expect((log.match(/straightedge log trimmed/g) ?? []).length).toBe(1);
    expect(countEntries(log)).toBe(LOG_KEEP_ENTRIES);
  });

  it("is idempotent on an already-trimmed log", () => {
    const once = trimLog(logOf(LOG_KEEP_ENTRIES + 7));
    const twice = trimLog(once.text);
    expect(twice.text).toBe(once.text);
    expect(twice.droppedEntries).toBe(0);
    expect(twice.totalDroppedEntries).toBe(once.totalDroppedEntries);
  });
});

describe("trimLog: the byte and line backstop", () => {
  it("drops under the window when few entries are enormous", () => {
    // Ten entries, so the entry window alone would keep all of them, and the
    // read tool could never see the whole thing in one call.
    const huge = logOf(10, "y".repeat(8 * 1024));
    expect(byteLength(huge)).toBeGreaterThan(LOG_READ_MAX_BYTES);
    const out = trimLog(huge);
    expect(out.droppedEntries).toBeGreaterThan(0);
    expect(countEntries(out.text)).toBeLessThan(10);
    expect(byteLength(out.text)).toBeLessThanOrEqual(LOG_READ_MAX_BYTES);
  });

  it("holds the line cap even when the byte cap is satisfied", () => {
    // Many short lines: well under 32 KiB, well over 800 lines.
    const wordy = logOf(10, "z\n".repeat(200));
    expect(byteLength(wordy)).toBeLessThan(LOG_READ_MAX_BYTES);
    const out = trimLog(wordy);
    expect(out.text.split("\n").length).toBeLessThanOrEqual(LOG_READ_MAX_LINES + 1);
    expect(out.droppedEntries).toBeGreaterThan(0);
  });

  it("truncates INSIDE the budget when one entry alone is too big", () => {
    // Dropping older entries cannot help here: there is only one.
    const out = trimLog(entry("assistant", 1, "w".repeat(LOG_READ_MAX_BYTES * 2)));
    expect(byteLength(out.text)).toBeLessThanOrEqual(LOG_READ_MAX_BYTES);
    expect(out.text).toContain("[truncated:");
    expect(out.text).toContain("straightedge#131");
    expect(out.droppedBytes).toBeGreaterThan(0);
  });

  it("carries a fragment with no heading instead of deleting it", () => {
    // Should not occur. A retention routine that quietly removes something it
    // did not expect is worse than one that keeps it.
    const out = trimLog(`stray preamble\n\n${logOf(2)}`);
    expect(out.text).toContain("stray preamble");
  });

  it("handles an empty log", () => {
    const out = trimLog("");
    expect(out.text).toBe("");
    expect(out.droppedEntries).toBe(0);
  });
});

describe("the shipped Durable Object, against its real storage", () => {
  const SESSION = "retention-live";

  it("bounds log.md across more asks than the window holds", async () => {
    const asks = LOG_KEEP_ENTRIES + 6;
    for (let i = 0; i < asks; i += 1) {
      const res = await call(goodAsk({ session: SESSION, question: `question ${i}` }));
      expect(res.status).toBe(200);
    }

    const log = await logFor(SESSION);

    // The instrument could have produced a positive: the asks really did
    // append, and the log really does hold the most recent exchange.
    expect(log).toContain(`question ${asks - 1}`);
    expect(log).toContain("assistant");

    // Two entries per ask (user + assistant), so this is well past the window.
    expect(countEntries(log)).toBeLessThanOrEqual(LOG_KEEP_ENTRIES);
    expect(byteLength(log)).toBeLessThanOrEqual(LOG_READ_MAX_BYTES);

    // The earliest questions are gone AND the file says they were dropped.
    expect(log).not.toContain("question 0\n");
    expect(log).toContain("straightedge log trimmed");
  });
});
