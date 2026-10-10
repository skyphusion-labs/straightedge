### A transform on a model-chosen symbol can no longer manufacture one (issue #197, p0-safety)

`"EURUſD".upper()` is `"EURUSD"`. Unicode uppercasing maps U+017F LATIN SMALL
LETTER LONG S onto ASCII `S`, so a model reply naming an instrument that does
not exist reached the desk as a staged BUY on one that does. Measured through
the shipped functions on merged `main`:

```
sent='EURUſD'  ->  parsed='EURUSD'  action='buy'  advice_allows=True
```

The string is a valid `["string","null"]`, carries no brace, raises no schema
violation and survives `_JSON_TAIL`. The ligatures `ff`, `fi` and `st`, the
dotless `i`, and `ß` (which expands to `SS`) all transform the same way.

**The same shape as the brace defect #181 fixed, one character different:** a
repair on `symbol` turns a NAMED REFUSAL into an order.

**#181's own guard could not see it, and that is worth knowing because the
guard reads like a general safety net.**
`test_a_braced_symbol_is_not_more_permissive_than_the_bare_parser` compares the
structured path against the bare parser, and both transform identically here:
a comparison between two paths is blind to a defect they share. Fixing this by
extending that comparison would have produced a green test over a live defect.

**The rule is ASCII BEFORE the transform, applied at every site that can
transform, and it is not a codepoint blocklist.** `isascii()` separates every
member of this family from every legitimate instrument name, and the next
case-mapping character is always one nobody enumerated. Four sites, each
sufficient on its own to have kept the defect alive:

- `config.advice_allows` refuses a non-ASCII name itself, rather than trusting a
  caller to have checked. The issue measured it returning True with no parser
  involved at all.
- `llm.parse_advice` leaves the name EXACTLY as sent instead of uppercasing it.
  `grok` and `computer` have no schema gate, so the parser is the only gate
  they have. The name is not blanked either: a `None` symbol makes the desk skip
  its staging block in silence, and the whitelist refusal is the loud answer.
- `llm._schema_violations` reports `symbol ... is not ASCII` and forces `hold`,
  so the structured path SAYS what was wrong instead of leaving an operator to
  infer it from a refusal further down. A model emitting a name outside the
  instrument vocabulary it was given is also evidence the schema constraint did
  not apply, which is that function's whole subject.
- `desk` names the string the MODEL sent in the `symbol_not_allowed` record and
  in the chat line. It used to log `advice.symbol.upper()`, so the refusal for
  `EURUſD` would have read `EURUSD`: a named refusal for an instrument that IS
  allowed, which looks like a bug in the whitelist rather than a rejected reply.

**The operator's own whitelist is held to the same rule**, because the claim in
`advice_allows` was not true without it: the config loader did
`[str(x).upper() for x in advice_names]`, so a typo of `EURUſD` in
`advice.symbols` became `EURUSD` before any gate could filter it, silently
widening the list to a symbol nobody typed. It now stays as typed and matches
nothing, which is a visible failure rather than an invisible widening.

`eurusd` still works throughout. An ASCII case fold is the same instrument, and
that is why the rule is ASCII-before-transform rather than no transform at all.

Thirty cases pin it, including the controls that keep the benign path alive and
a pin on the two-path agreement that made #181's comparison silent. One of the
six characters in the corpus reaches a symbol in the SHIPPED whitelist and that
is said out loud in the corpus comment rather than left to look like six
exploits; all six share the parser transform, which is the mechanism the rule
binds.
