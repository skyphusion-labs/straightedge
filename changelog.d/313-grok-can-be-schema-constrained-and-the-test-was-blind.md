### A comment said grok cannot constrain its output, and the test guarding it was blind (issue #313)

`llm.py` carried this as the justification for a per-provider asymmetry on the
advice safety path:

```
# Only the claude path gets this. `grok` and `computer` cannot
# constrain their output, and a provider that cannot must not
# silently lose the parser, so they keep `parse_advice` alone.
```

For `grok` that is false. xAI documents structured outputs on the same
OpenAI-compatible endpoint `grok_url` already defaults to, as a top-level
`response_format: {"type": "json_schema", "json_schema": {...}}`, and its
documented subset accepts every construct `ADVICE_FORMAT` uses: the closed
`action` enum, `additionalProperties: false`, and `{"type": ["string",
"null"]}`, which is its own documented spelling for a nullable field.

**A wrong capability claim is worse here than no comment.** `config.py` ships
`provider: str = "grok"`, so this sentence was the stated reason the DEFAULT
provider has a parsed-and-hoped `action` while `claude` has a schema-guaranteed
one, and it documented that gap as impossible to close. Nobody re-opens a
question the code says has no answer. The comment now states the real reason,
which is that one live verification against api.x.ai is missing and that
sending an unverified body would 400 the advice path for the default provider
of a desk being demonstrated live.

**The test named for this claim could not observe it, and that is worth more
than the comment.** `test_grok_keeps_the_parser_and_is_sent_no_schema`
asserted `"output_config" not in body`. That is Anthropic's parameter name, so
the test pinned "grok is not sent ANTHROPIC's schema" while being named for
"grok is sent no schema". Measured by injecting a real xAI `response_format`
into the grok request body and re-running it:

```
CONTROL   unmodified:                    2 passed   exit 0
INJECTED  grok sends response_format:    2 passed   exit 0   <- blind
```

The same injection against the rewritten test:

```
INJECTED  grok sends response_format:    1 failed   exit 1
  AssertionError: grok was sent a schema: ['response_format',
    'response_format.type=json_schema', 'response_format.json_schema']
```

`_schema_mentions` now walks the whole request body for every documented
spelling (`output_config`, `response_format`, `text.format`, and the
`json_schema` type value all three share) rather than one key at one level, so
a schema sent under a shape nobody listed is still caught by the value it has
to carry. Two controls come with it, because a checker that returns an empty
list is otherwise indistinguishable from one that can never return anything:
one drives all three vendor shapes plus a clean body, and one asserts the
checker fires on the SHIPPED `_claude` body rather than on a hand-written dict.

The negative tests are now pinning a deliberate present state rather than a
vendor limitation, and that is the intended cost: sending grok a schema has to
edit that assertion, which is what stops the wire change landing half-done.

**Not in this change, and named so it is not mistaken for done:** `grok` is
still sent no schema and `_schema_violations` still does not run on its reply.
Both wait on one live probe against api.x.ai, and the second carries a decision
that the probe informs, since requiring every tail key on a provider with no
server-side guarantee of completeness would force `hold` on terse-but-valid
replies. #313 stays open for it.
