/**
 * Retention for `/workspace/log.md`. straightedge#131.
 *
 * WHY A BOUND EXISTS AT ALL
 *
 * It is not the storage limit. A Durable Object's SQLite would take a very
 * long time to fill with text. The wall is `DeskAgent.ask()`, which reads the
 * WHOLE log and writes the WHOLE log back, twice per turn: once to append the
 * question before the model runs, once to append the reply after. So per-turn
 * I/O, memory and latency grow linearly with everything the session has ever
 * said, inside a single Worker request, and that is the thing that gets slower
 * every day with nobody watching. The desk is now armed on auto and
 * unattended, so "nobody watching" is the normal case rather than the unusual
 * one.
 *
 * WHERE THE NUMBERS COME FROM. Both are already declared elsewhere; neither is
 * a taste call, and neither is restated as a literal here.
 *
 * `LOG_KEEP_ENTRIES` = 40 is the desk's own `llm.KEEP_TURNS`, whose docstring
 * reads "Bound to KEEP_TURNS messages". It counts ONE ROLE'S MESSAGE per unit
 * (`_remember` is called twice per ask and then slices `[-KEEP_TURNS:]`), and
 * an entry here is also one role's message, so the unit and the number match
 * exactly. The desk reached 40 twice independently: `journal.advice.json`
 * keeps 40 and `engine.advice_history` is `journal.tail(40)`. This is the same
 * conversation; it gets the same window.
 *
 * `LOG_READ_MAX_BYTES` and `LOG_READ_MAX_LINES` are the read tool's OWN limits,
 * declared in `desk-agent.ts` and imported from here so there is one
 * declaration rather than two that can drift. Holding the log at or under them
 * buys something specific: `read` with no `offset` returns the FIRST lines of
 * a file, so on a long log the model's unprompted read of `log.md` returns the
 * OLDEST turns while the system prompt tells it this is the conversation log.
 * A log that fits in one read cannot have a stale prefix.
 *
 * WHY NOT A TIME LIMIT. The DO sets no alarm and has no clock the operator
 * controls, so a trim could only happen on the next turn anyway. Worse, it
 * scales with the wrong thing: an idle session would hold its whole history
 * forever and a busy one would lose the morning by lunchtime. Turns and bytes
 * are what the reader and the writer actually cost.
 *
 * A TRIM IS NEVER SILENT. Same discipline as the Telegram truncation notice in
 * straightedge#128: the drop is named in the artifact itself, on the first
 * line, with CUMULATIVE totals, so the model reads it and so a reader can
 * always tell a short log from a trimmed one. `MARKER_RE` parses those totals
 * back on the next trim, and `trimLog` is idempotent across repeats.
 */

export const LOG_READ_MAX_BYTES = 32 * 1024;
export const LOG_READ_MAX_LINES = 800;
export const LOG_KEEP_ENTRIES = 40;

/** Bytes held back from the budget so the marker line always fits. */
const MARKER_RESERVE_BYTES = 256;

const MARKER_RE =
  /^<!-- straightedge log trimmed: (\d+) earlier entries \((\d+) bytes\) dropped[^>]*-->\n/;

const encoder = new TextEncoder();

export function byteLength(s: string): number {
  return encoder.encode(s).length;
}

export type TrimResult = {
  /** What to write back. Carries the marker when anything has ever been dropped. */
  text: string;
  /** Entries dropped by THIS call. */
  droppedEntries: number;
  /** Bytes dropped by THIS call. */
  droppedBytes: number;
  /** Cumulative over the life of the session, marker included. */
  totalDroppedEntries: number;
  totalDroppedBytes: number;
};

function marker(entries: number, bytes: number): string {
  return (
    `<!-- straightedge log trimmed: ${entries} earlier entries (${bytes} bytes) ` +
    `dropped; keeping the most recent ${LOG_KEEP_ENTRIES} entries within ` +
    `${LOG_READ_MAX_BYTES} bytes and ${LOG_READ_MAX_LINES} lines, so one read ` +
    `sees the whole log. straightedge#131 -->\n`
  );
}

/** Split a previously written log into its marker totals and the entries after it. */
function splitMarker(log: string): { entries: number; bytes: number; body: string } {
  const m = MARKER_RE.exec(log);
  if (!m) {
    return { entries: 0, bytes: 0, body: log };
  }
  return {
    entries: Number(m[1]),
    bytes: Number(m[2]),
    body: log.slice(m[0].length),
  };
}

/**
 * Split the body on `## ` headings, each entry keeping its own heading.
 *
 * Anything before the first heading is returned as a leading fragment rather
 * than silently dropped: it should not exist, and a retention routine that
 * quietly deletes something it did not expect is worse than one that carries
 * it. It is counted against the budget like any other text.
 */
function splitEntries(body: string): string[] {
  if (!body) return [];
  const out: string[] = [];
  const re = /^## /gm;
  const starts: number[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(body)) !== null) {
    starts.push(m.index);
  }
  if (starts.length === 0) return [body];
  if (starts[0] > 0) out.push(body.slice(0, starts[0]));
  for (let i = 0; i < starts.length; i += 1) {
    const end = i + 1 < starts.length ? starts[i + 1] : body.length;
    out.push(body.slice(starts[i], end));
  }
  return out;
}

function lineCount(s: string): number {
  if (!s) return 0;
  let n = 1;
  for (const ch of s) {
    if (ch === "\n") n += 1;
  }
  return n;
}

/**
 * Shrink ONE entry that is over budget on its own, naming the loss inline.
 *
 * Reached when a single question or reply is bigger than the whole budget, so
 * dropping older entries cannot help. The notice is fitted INSIDE the budget
 * rather than appended to it, which is exactly how `telegram._outbound_chunks`
 * handles the same problem: a notice that pushes the result back over the
 * limit has not solved anything.
 */
function clampEntry(entry: string, budget: number): { text: string; dropped: number } {
  const notice = (n: number) => `\n\n[truncated: ${n} more chars, straightedge#131]\n\n`;
  const room = budget - byteLength(notice(byteLength(entry)));
  if (room <= 0) {
    return { text: notice(byteLength(entry)), dropped: byteLength(entry) };
  }
  let kept = "";
  let used = 0;
  for (const ch of entry) {
    const size = byteLength(ch);
    if (used + size > room) break;
    kept += ch;
    used += size;
  }
  return { text: kept + notice(byteLength(entry) - used), dropped: byteLength(entry) - used };
}

/**
 * Apply retention. Pure: the caller does the writing.
 *
 * Order is deliberate. Entries first, because the entry count is the bound
 * that matches the desk's own window and the one an operator can reason about;
 * then bytes and lines, which are the read tool's limits and act as the
 * backstop for the case the entry count cannot see, a small number of enormous
 * entries.
 */
export function trimLog(log: string): TrimResult {
  const prior = splitMarker(log);
  let totalEntries = prior.entries;
  let totalBytes = prior.bytes;
  let droppedEntries = 0;
  let droppedBytes = 0;

  let kept = splitEntries(prior.body);

  if (kept.length > LOG_KEEP_ENTRIES) {
    const cut = kept.slice(0, kept.length - LOG_KEEP_ENTRIES);
    droppedEntries += cut.length;
    droppedBytes += byteLength(cut.join(""));
    kept = kept.slice(kept.length - LOG_KEEP_ENTRIES);
  }

  const budget = LOG_READ_MAX_BYTES - MARKER_RESERVE_BYTES;
  while (
    kept.length > 1 &&
    (byteLength(kept.join("")) > budget || lineCount(kept.join("")) > LOG_READ_MAX_LINES)
  ) {
    const gone = kept.shift() as string;
    droppedEntries += 1;
    droppedBytes += byteLength(gone);
  }

  if (kept.length === 1 && byteLength(kept[0]) > budget) {
    const clamped = clampEntry(kept[0], budget);
    kept = [clamped.text];
    droppedBytes += clamped.dropped;
  }

  totalEntries += droppedEntries;
  totalBytes += droppedBytes;

  const body = kept.join("");
  const text = totalEntries > 0 || totalBytes > 0 ? marker(totalEntries, totalBytes) + body : body;
  return {
    text,
    droppedEntries,
    droppedBytes,
    totalDroppedEntries: totalEntries,
    totalDroppedBytes: totalBytes,
  };
}
