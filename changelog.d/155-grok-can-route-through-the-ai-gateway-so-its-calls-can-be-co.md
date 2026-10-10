### `grok` can route through the AI Gateway, so its calls can be counted (issue #155)

`AI_PROVIDER=computer` was the only path the gateway saw, so for the two
providers most likely to be live we had no call count, no token count and no
cost. `claude` already routed by URL; `grok` did not, and sent
`Authorization: Bearer` unconditionally.

- **`grok` now routes by HOST, calling the same `_is_cf_gateway` the `claude`
  path calls.** Point `advice.grok_url` at a Cloudflare AI Gateway and it
  authenticates with `cf-aig-authorization` and sends no xAI key, because
  Unified Billing supplies the provider credential. Point it at `api.x.ai` and
  it stays direct BYOK. The predicate is CALLED rather than re-spelled, so a
  URL-bypass shape fixed for one provider is fixed for both.
- **Defaults are unchanged and pinned by a test.** A self-hoster who changes
  nothing keeps their own key going straight to the provider, and neither
  provider acquires a Cloudflare dependency.
- **The deciding property is where the counter lives, not the cost figure.**
  A day of zero AI spend on a config that should call the model every bar is a
  signal, and a counter we increment ourselves cannot raise it: the code that
  stopped calling the model is the same code that would stop counting. That
  rules out a locally computed estimate independently of latency.
- **One test body now drives BOTH providers over the same URL-bypass table**,
  rather than a second table that could be fixed on one side only. Reverting
  the `grok` branch reds exactly the four gateway cases for `grok` and zero for
  `claude`.
- README states the consequence of going direct, which it previously did not:
  the mechanism was documented and the fact that it leaves usage unmeasurable
  was not.
