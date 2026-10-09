import { createOpenAI } from "@ai-sdk/openai";
import { Workspace, type DurableObjectStorageLike } from "@cloudflare/computer";
import { createAITools } from "@cloudflare/computer/tools";
import { generateText, stepCountIs } from "ai";
import { DurableObject } from "cloudflare:workers";
import type { Env } from "./env";
import {
  LOG_READ_MAX_BYTES,
  LOG_READ_MAX_LINES,
  trimLog,
} from "./log-retention";

export const SYSTEM = [
  "You are a risk desk, not a tipster. One account. One book.",
  "Working memory: notes.md, log.md, snapshot.md, history.json. Read them. Update notes.md with levels and rules.",
  "Use bid/ask/spread/ATR/ADX/EMA, positions, orders, daily_loss room, drawdown room, and fills.",
  "Never claim consistent profits. Do not size orders. The risk engine sizes and can refuse. You do not send.",
  "If they ask for a trade: name price, stop, target, and why the stop is invalidation. Hold if spread vs ATR is poor. Do not stack correlated majors the same way.",
  "Conservative means defined SL, no chase, no martingale, no averaging into a loser.",
  "Always set sl and tp on buy/sell. Limit XOR stop. Close needs ticket.",
  "If they ask for general portfolio or book advice: do not invent a trade. Cover allocation, correlation, unused risk room, and what not to do. JSON action must be hold unless they clearly asked to execute.",
  "End with one JSON object, no fence:",
  '{"action":"buy"|"sell"|"close"|"hold","symbol":"EURUSD"|null,"sl":number|null,"tp":number|null,"limit":number|null,"stop":number|null,"ticket":number|null,"summary":"one line"}',
].join(" ");

type AskBody = {
  session?: string;
  question?: string;
  context?: string;
  model?: string;
  history?: unknown;
};

export class DeskAgent extends DurableObject<Env> {
  readonly workspace: Workspace;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.workspace = new Workspace({
      storage: ctx.storage as DurableObjectStorageLike,
    });
  }

  async fetch(request: Request): Promise<Response> {
    if (request.method !== "POST") {
      return json({ error: "POST only" }, 405);
    }
    let body: AskBody;
    try {
      body = (await request.json()) as AskBody;
    } catch {
      return json({ error: "invalid json" }, 400);
    }
    const question = String(body.question || "").trim();
    const context = String(body.context || "");
    const history = Array.isArray(body.history) ? body.history : [];
    if (!question) {
      return json({ error: "question required" }, 400);
    }
    try {
      const text = await this.ask(question, context, String(body.model || ""), history);
      return json({ text });
    } catch (err) {
      const msg = err instanceof Error ? err.message : "ask failed";
      return json({ error: msg }, 502);
    }
  }

  async ask(
    question: string,
    context: string,
    modelId: string,
    history: unknown[],
  ): Promise<string> {
    const token = this.env.CF_AIG_TOKEN;
    const account = this.env.CF_ACCOUNT_ID;
    const gateway = this.env.AI_GATEWAY_ID;
    if (!token || !account || !gateway) {
      throw new Error("AI Gateway is not configured (CF_AIG_TOKEN, CF_ACCOUNT_ID, AI_GATEWAY_ID)");
    }
    await this.workspace.fs.mkdir("/workspace", { recursive: true });
    await this.workspace.fs.writeFile("/workspace/snapshot.md", context || "(no snapshot)");
    await this.workspace.fs.writeFile(
      "/workspace/history.json",
      `${JSON.stringify(history, null, 2)}\n`,
    );
    const prev = await readUtf8(this.workspace, "/workspace/log.md");
    const stamp = new Date().toISOString();
    await this.appendToLog(prev, `## ${stamp} user\n\n${question}\n\n`);

    // Docs: REST API at api.cloudflare.com, Authorization + cf-aig-gateway-id.
    // Unified Billing: do not send a provider key. Model ids are author/model (xai/grok-4.6).
    // https://developers.cloudflare.com/ai-gateway/usage/rest-api/
    const openai = createOpenAI({
      apiKey: token,
      baseURL: `https://api.cloudflare.com/client/v4/accounts/${account}/ai/v1`,
      headers: {
        "cf-aig-gateway-id": gateway,
        "cf-aig-collect-log-payload": "false",
        "cf-aig-metadata": JSON.stringify({
          bot: "straightedge",
          surface: "computer",
        }),
      },
    });
    const model = openai.chat(modelId || this.env.ADVICE_MODEL || "xai/grok-4.6");
    const tools = createAITools({
      workspace: this.workspace,
      // Declared in log-retention.ts, not here: the retention bound is
      // DERIVED from these two, so a second literal could drift and silently
      // stop the log fitting in one read (straightedge#131).
      read: { maxBytes: LOG_READ_MAX_BYTES, maxLines: LOG_READ_MAX_LINES },
    });
    const result = await generateText({
      model,
      system: SYSTEM,
      prompt: [
        "Desk snapshot is /workspace/snapshot.md.",
        "Journal history is /workspace/history.json.",
        "Durable notes are /workspace/notes.md. Read and update them.",
        "Conversation log is /workspace/log.md.",
        `Operator: ${question}`,
      ].join("\n"),
      tools,
      stopWhen: stepCountIs(8),
    });
    const text = result.text || "";
    const after = await readUtf8(this.workspace, "/workspace/log.md");
    await this.appendToLog(after, `## ${stamp} assistant\n\n${text}\n\n`);
    return text;
  }

  /**
   * Append one entry and apply retention. straightedge#131.
   *
   * Every write to `log.md` goes through here. The file was appended to with
   * no cap, no rotation and no delete route, and the Worker serves only
   * `/health` and `/ask`, so nothing could trim it from outside either. The
   * cost was not storage: `ask()` reads and rewrites the WHOLE file twice per
   * turn, so turn latency and memory grew with every question the session had
   * ever asked.
   *
   * A trim is logged as well as marked in the file. `console.log` reaches
   * Workers observability and `wrangler tail`, which is the only channel this
   * Durable Object has: it cannot reach the desk's journal, and putting the
   * notice in the reply would mean writing it into advice text.
   */
  private async appendToLog(previous: string, entry: string): Promise<void> {
    const trimmed = trimLog(`${previous}${entry}`);
    if (trimmed.droppedEntries > 0 || trimmed.droppedBytes > 0) {
      console.log(
        JSON.stringify({
          event: "log_trimmed",
          dropped_entries: trimmed.droppedEntries,
          dropped_bytes: trimmed.droppedBytes,
          total_dropped_entries: trimmed.totalDroppedEntries,
          total_dropped_bytes: trimmed.totalDroppedBytes,
        }),
      );
    }
    await this.workspace.fs.writeFile("/workspace/log.md", trimmed.text);
  }
}

async function readUtf8(ws: Workspace, path: string): Promise<string> {
  try {
    const v = await ws.fs.readFile(path, "utf8");
    return typeof v === "string" ? v : "";
  } catch {
    return "";
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}
