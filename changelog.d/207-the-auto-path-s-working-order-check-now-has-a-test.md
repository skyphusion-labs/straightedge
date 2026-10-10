### The auto path's working-order check now has a test (issue #207)

`risk.evaluate` is handed `orders=orders` from two sites and only one of them
had a test. Measured on `main` at `937d0d6`, mutating each to `[]`:

| mutation | stop-guard | working-orders | new auto-path |
| --- | --- | --- | --- |
| `preview`'s `orders=orders` (engine.py:1307) | green | **RED** | green |
| `_act`'s `orders=orders` (engine.py:1192) | green | green | **RED** |
| `risk.py`'s `ours_orders` (risk.py:553) | green | **RED** | **RED** |

The second row is the gap: **dropping working orders from the AUTO path's risk
evaluation was caught by nothing.** The first and third rows are what make the
second one a gap rather than a guess, because each suite reds on exactly one
call site and the shared point reds both, so the two suites are blind to each
other's caller and the new one genuinely drives `_act`.

If it regressed, resting working orders would stop counting toward
`max_positions`, `already_in_symbol` and the currency limit **for auto entries
only**, which is the defect
`tests/test_working_orders_count_as_exposure.py` exists to prevent, reproduced
in the one path that suite does not reach. Not urgent: `auto` is off by default
and separately gated, so no operator was exposed. It is a shipped feature on a
repo pointed at real money and the cost of closing it was one test.

**The gate under test is `max_positions`, and `already_in_symbol` could not be
used.** `_act` carries its own same-symbol check before it ever calls
`evaluate`, so a same-symbol order is refused earlier and a test built on that
reason would measure the earlier check instead, passing whatever `orders=` was
handed over. The resting order therefore sits on a different symbol.
`currency_exposure` is covered too, because one assertion through one gate is a
single point and the two read the same argument by different routes.

**The preconditions that make each red possible are stated in the suite**, not
left implicit: a real resting order placed through the desk on another symbol,
an auto tick that actually reaches `evaluate`, and a slot limit the resting
order alone fills. Drop any one and the mutation is EQUIVALENT and the suite is
green for a reason unrelated to the gate. One case asserts precondition two
directly, by raising the limit and requiring the same tick to open, since every
other assertion here is vacuous if `_act` returns early.

**One fixture bug, caught by its own case rather than by review.** The currency
case first committed a second leg with `/buy USDCHF`, which is LONG USD and
therefore CANCELLED the resting order's short-USD leg: the cap was never
reached and the case failed with no refusal at all. It uses `AUDUSD` now, and
the comment says why, because a fixture that tests a state it did not intend is
the failure this repo keeps finding.
