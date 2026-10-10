### A non-finite number no longer reaches the journal as a number (issue #231)

`_num("9" * 5000)` returns `inf`, so a model reply could put `sl`, `tp`,
`limit` or `stop` into an `advice_turn` row as a non-finite value, and `rr`
reached `reject` rows the same way. The order path already refused such
values (#208, #211, #219, #221); this was the RECORD, which is the evidence
the reconciliation work depends on.

- **The value is written as `nonfinite:<value>`, a string**, and the row
  carries a `nonfinite` list naming the dotted paths converted. Merged with
  a caller-supplied list rather than replacing it; the first version of the
  fix clobbered it, which bites only when a caller uses the key AND the row
  carries a non-finite value.
- **Applied by the WRITER, after redaction and immediately before
  serialisation**, so it covers every row and every field rather than one
  producer. `rr: Infinity` already reached `reject` rows that `_num` does
  not author, so a producer-side fix would have passed its own test while
  the same token kept shipping.
- **Recursive**, which the issue did not name: `_jsonable` is applied per
  top-level field and does not descend, so a value nested in a dict or list
  reached disk untouched. Measured on the real `history_preflight` shape,
  where `symbols[1].atr` is a path a non-finite value can occupy.
- **`allow_nan=False` makes the claim structural**: a value that escaped
  marking cannot reach the bytes. It degrades to a row naming the event and
  the failure rather than raising into the desk, because losing an
  append-only audit row is worse than writing a degraded one.
- **Not clipped and not omitted.** `jq` parses a bare `Infinity`, reports
  `isinfinite` true, and serialises `1.7976931348623157e+308`, so a
  reconciliation through the obvious tool reports a price the desk never
  saw with no error anywhere. Clipping is what jq already does. Omitting
  cannot be told from the model saying nothing.
- The gate reads the written BYTES and uses `node` and `jq` as well as
  Python, because `json.loads` ACCEPTS the bad line: a Python-only
  round-trip passes today and would have passed before the fix.
