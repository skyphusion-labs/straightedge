# mt5-risk-agent

The agent is the Cloudflare Computer worker.
It is the desk's advice brain.
The desk is Telegram chat commands.
The bot is the Python process on this computer.
The gateway is Cloudflare AI Gateway `mt5-risk-bot`.

Working memory is the workspace filesystem (`/workspace/notes.md`, `log.md`, `snapshot.md`, `history.json`).
Computer tools are `read`, `write`, `edit`, `ls`, and `grep`.
Inference is Unified Billing on the gateway.
It is not provider BYOK.

Live:

- Worker: `https://mt5-risk-agent.skyphusion.workers.dev`
- Health: `GET /health` -> `ok`
- Ask: `POST /ask` with `Authorization: Bearer ADVICE_TOKEN` and a `session` in the body
- The gateway: `mt5-risk-bot` (account `fabcb25d9c7eb087110ec474a03e50d2`)
- Model: `xai/grok-4.6`

The agent depends on `@cloudflare/computer`, which Cloudflare ships as an early preview with unstable APIs.
The Production/Stable classifier in `pyproject.toml` covers the bot, not the agent.
The bot still runs next to MT5.

## Inference path (docs)

New calls use the gateway REST API, not `/compat`:

```
POST https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions
Authorization: Bearer CF_AIG_TOKEN
cf-aig-gateway-id: mt5-risk-bot
cf-aig-collect-log-payload: false
{"model":"xai/grok-4.6","messages":[...]}
```

See [REST API](https://developers.cloudflare.com/ai-gateway/usage/rest-api/) and
[Unified Billing](https://developers.cloudflare.com/ai-gateway/features/unified-billing/).

WARNING
Do not put `CF_AIG_TOKEN` on `gateway.ai.cloudflare.com` as `Authorization`.
The gateway forwards that header to xAI as a provider key.
Unified Billing is skipped.

## Session key

`session` names the Durable Object that answers, so it is the boundary between
one caller's working memory and another's. The Worker validates it before it
addresses anything.

- It must be PRESENT and a JSON string. A number, a boolean, `null`, an array or
  an object is refused, not converted to a string.
- 1 to 64 characters, from letters, digits, dot, underscore and hyphen. No
  spaces, no newlines, no path or URL separators, no unicode.
- There is no implicit fallback. An absent or empty `session` is refused; a
  caller that wants the shared desk sends `default` and means it.

A refused key gets `400 {"error":"invalid session"}` and no Durable Object is
created. A non-POST `/ask` gets `405 {"error":"POST only"}` from the Worker
itself now rather than from the Durable Object, for the same reason: the status
and body are unchanged, but nothing is addressed to produce them.

`ADVICE_SESSIONS` is optional and unset by default. Set it on the deployment to
a comma-separated list and those are the only keys served; leave it unset and
the rule above is the only constraint. A list that is set but holds no usable
entry serves nobody, which is the safe direction for a typo.

The bot posts the Telegram chat id, which fits the rule. `tests/test_desk.py`
pins that caller side, and `agent/test/session.test.ts` pins this side.

## Tests

```
npm ci
npm run typecheck
npm test
```

The suite runs in workerd via `@cloudflare/vitest-pool-workers`, not in node, so
the Worker, the `DeskAgent` Durable Object and its SQLite-backed workspace all
execute for real. CI runs it as the `agent-test` job.

The AI Gateway is the one hop the suite cannot call, so it is the one seam:
`vitest.config.ts` supplies an `outboundService` that answers the gateway and
returns 599 for anything else, which makes unexpected egress a failure rather
than a silent pass. Nothing under `agent/src/` is replaced or stubbed. The fake
gateway echoes back the headers and model it observed, which is how the outbound
contract is asserted; it never echoes the credential value.

Two properties of the runner are worth knowing before adding tests.

- A Durable Object receives its bindings from the runtime, not from the `env`
  object its caller holds. Overriding `CF_AIG_TOKEN` at the entry Worker does
  nothing to `DeskAgent`. Use the `withDeskEnv` helper for DESK-side variables.
- Storage is isolated per test FILE, not per test. `reset()` does not clear the
  namespace listing. A test that needs "no Durable Object exists at all" belongs
  in a file where nothing else authorizes a request; `test/auth-ordering.test.ts`
  is that file, and it says so at the top.

`agent/package.json` pins `overrides.miniflare` because the version the test pool
depends on ships a workerd older than this Worker's `compatibility_date`. Without
the override the runtime refuses to start. Raise the override, do not lower the
compatibility date: the suite has to run the runtime that ships.

## Dependency advisories with no patched version

`GHSA-hp3w-g68c-fv3c` (sprintf-js, denial of service, medium) is **dismissed as
not present in the deployed Worker**, straightedge#191. Recorded here because a
dismissal with no reasoning behind it is the same defect as a check that cannot
fail, and because the alert will come back if anyone re-enables it.

**A bump cannot fix it. The first patched version is `null`,** so `npm update`, a
Dependabot PR and an `overrides` pin all have nothing to point at. The
`overrides` block above is not applicable.

It enters the graph here (`npm ls sprintf-js --all`):

```
mt5-risk-agent -> @cloudflare/computer@0.4.0 -> just-bash@3.4.2 -> sprintf-js@1.1.3
```

`just-bash` is the shell behind `@cloudflare/computer`'s `exec` tool, and sprintf
is reached from that shell's `printf` builtin and from `awk`'s `sprintf`
function. Three measurements, in increasing order of how much they settle:

1. **The amplifier is WIDTH, not precision.** `%.101f` throws `RangeError`
   because V8 caps `toFixed` at 100 digits; `%100000000d` returns a 100 MB
   string instantly, because sprintf-js pads with `pad_char.repeat(width - len)`.
   So the advisory's own wording does not describe the reachable amplifier in
   this engine, and anyone re-checking it with a precision specifier will
   wrongly conclude there is nothing there.
2. **`just-bash` bounds it before sprintf sees it.** Width and precision are both
   validated against a 67108864-byte ceiling at format-spec PARSE time, and the
   shell answers `bash: format width limit exceeded (67108864 bytes)`. That holds
   on the specs its own fast path handles AND on the ones that fall through to
   raw sprintf (`%+Ns`, `%Nj`, `%Nv`, `%NT`, `%Nb`), all checked.
3. **None of it is in the deployed artifact.** `npx wrangler deploy --dry-run
   --outdir <dir>` produces the 2.1 MB bundle wrangler would upload, and
   `sprintf`, `vsprintf` and sprintf-js's own internal `not_primitive` and
   `numeric_arg` keys appear **zero** times in it. The same grep finds `sprintf`
   5 times in `node_modules/just-bash/dist/bundle/index.cjs`, and finds
   `createAITools`, `Workspace` and `generateText` in the bundle, so the
   instrument can produce a positive. esbuild drops `just-bash` because nothing
   imports an exec backend.

**The vantage: no vantage reaches it, and that does not rest on
authentication.** `/ask` does require `Bearer ADVICE_TOKEN`, but the dismissal
does not depend on that. `deskTools()` in `src/desk-agent.ts` calls
`createAITools` WITHOUT a `shell` option, and that option alone is what gates
the `exec` tool: the installed source reads
`if (options.shell === void 0) return void 0;` and the type says "Omit for no
exec tool". So the tool set is `delete, edit, find, grep, ls, read, write` and
no shell. The capability is ABSENT rather than guarded, so an authenticated
caller, the model itself, and a stranger all reach the same place: there is
nothing to call.

## What would invalidate this, and how to re-check it

**Two changes are needed for a model-reachable shell, and they are seen by
different instruments. Watch for both.**

| change | effect | caught by |
| --- | --- | --- |
| a `shell` option on `createAITools` | exposes the `exec` TOOL | `test/tools.test.ts`, on every push |
| a registered exec backend | puts just-bash's CODE in the bundle | the `wrangler deploy --dry-run` grep below |

`exec` is gated on the `shell` option ALONE. Measured: this Workspace registers
no backends at all, and passing `shell` yields
`delete, edit, exec, find, grep, ls, read, write`. `backends` on the `Workspace`
constructor is the other half only: on its own it adds no tool and nothing the
model can call, so it deliberately does NOT red the test.

`test/tools.test.ts` asserts an ALLOW-LIST rather than the absence of the name
`exec`, because a tool called `shell` or `run` would reach `just-bash` just as
well, and it carries a positive control that builds a tool set WITH a shell and
checks the assertion rejects it. A test that cannot be shown failing is not a
control.

**The bundle half has to be re-checked by hand, and the zero is a MEASURED
negative rather than an absence.** The procedure is below as code, because the
obvious version of it DOES NOT WORK and would make you conclude the instrument
cannot fire.

**Importing the module is not enough. esbuild tree-shakes it.** Measured, four
variants, each bundled for real:

| mutation to `src/desk-agent.ts` | Total Upload | sprintf lines |
| --- | --- | --- |
| none, as shipped | 2118.84 KiB | 0 |
| `import { WorkerShellBackend } from ".../worker-shell";` alone | **2118.84 KiB** | **0** |
| that import plus a module-scope `void WorkerShellBackend;` | **2118.84 KiB** | **0** |
| the import plus a reference RETAINED through the exported function | 5119.18 KiB | 44 |
| the same, instantiated | 5119.19 KiB | 44 |

So a re-checker who adds the import, sees 2118.84 and concludes the grep cannot
go positive has been misled by the procedure rather than by the code. Use this,
which is the mutation the numbers below came from:

```ts
import { WorkerShellBackend } from "@cloudflare/computer/backends/worker-shell";
const _probe: unknown = new WorkerShellBackend({} as never);
export function deskTools(workspace: Workspace) {
  void _probe;        // retains it; without this the import is dropped
```

Then:

```
$ npx wrangler deploy --dry-run --outdir /tmp/b
                                                               shipped   with the backend
Total Upload                                                   2118.84      5119.19 KiB
grep -ci 'sprintf|vsprintf|not_primitive|numeric_arg' (LINES)        0               44
grep -oi  same pattern | wc -l                  (OCCURRENCES)        0              120
grep -c  'format width limit exceeded|printf: usage'  (LINES)        0                3
grep -o   same pattern | wc -l                  (OCCURRENCES)        0                4
```

**Each figure is labelled with the command that produces it, and that is not
pedantry.** Two reviewers of this file reported 44 against 120 and 4 against 3
for the same bundle and read it as a disagreement; it was `grep -c` counting
LINES against `grep -o | wc -l` counting OCCURRENCES, with neither stating
which. A bare count in a re-check procedure is a number the next person cannot
reproduce. The size also moves in its last decimal with the exact shape of the
mutation (5119.18 retained, 5119.19 instantiated, 5119.27 with a `shell` option
as well), so treat the megabyte as the signal and not the hundredths.

So the instrument can be shown going positive on exactly the change that would
invalidate this, which is what makes the shipped zero worth quoting.

## Secrets (never in git)

Agent secrets: `CF_AIG_TOKEN`, `ADVICE_TOKEN`.

1. Change to the agent directory.
   `cd agent`
2. Put the gateway token.
   `npx wrangler secret put CF_AIG_TOKEN`
3. Put the desk token.
   `npx wrangler secret put ADVICE_TOKEN`
4. Deploy.
   `npx wrangler deploy`

Laptop copy: `agent/.dev.vars` (0600, gitignored).
Source it.
Do not paste tokens into chat.

## Desk

1. Load the laptop token file.
   `set -a`
2. Source it.
   `source agent/.dev.vars`
3. Stop exporting.
   `set +a`
4. Point the bot at the agent.
   `export AI_PROVIDER=computer`
   `export ADVICE_URL=https://mt5-risk-agent.skyphusion.workers.dev/ask`

`/ask` and free text POST `{session, question, context, history, model}`.
`history` is `journal.tail` (JSON list). The agent writes it to `/workspace/history.json`.
Session is the Telegram chat id, and it is required. See Session key above.
The agent does not send trades.
