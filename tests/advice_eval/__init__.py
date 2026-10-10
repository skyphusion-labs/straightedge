"""Advice-model evaluation harness (straightedge#165).

WHAT THIS IS. A harness that grades the desk's own advice path against FIXED
snapshots, so three providers and three context arms can be compared on
identical input. It is a measuring instrument, not shipped desk code: it lives
under `tests/` because `pyproject.toml` puts `tests` on `pythonpath` and
because `ruff check src/ tests/` gates it, and it is deliberately outside
`[tool.coverage.run] source` and outside mypy's `files`, which both scope to
`src/straightedge`.

IT DOES NOT CALL A MODEL. `runner.py` drives the real `llm.Advisor` through an
INJECTED transport, and the only transport this package ships is
`runner.ReplayTransport`, which replays a recorded or synthesized reply. There
is no live transport here at all, so "no provider was called" is a property of
the code rather than a promise in a report. Running the matrix for real is a
separate change, gated on Conrad's spend ruling (#165's Spend section).

WHY `Advisor` AND NOT A REIMPLEMENTATION. The question #165 asks is about the
path `/ask` uses. `Advisor.ask` is that path, including `_claude`'s
`output_config.format` request, `structured_to_parseable`, the schema gate and
`parse_advice`. A harness that built its own request and its own parser would
measure a second implementation and tell us nothing about the desk.

THE ONE THING THIS HARNESS MUST NOT DO is report a reassuring result it
structurally could not have failed. `docs/TESTING.md` carries that lesson for
this repo, and it applies to the arms in particular: #165 was written when
`advice_context()` carried no aggregates, which is no longer true, so an
"Arm A vs Arm B" built from the issue's own words would have compared one
string against itself and reported "no lift" with the cause being that the two
arms were identical. `arms.py` asserts the arms DIFFER before anything is
graded, and `tests/test_advice_eval.py` carries a positive control per metric.
"""
