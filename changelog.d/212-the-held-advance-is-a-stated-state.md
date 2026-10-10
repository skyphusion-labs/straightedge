### The hour after a venue clock moves back is a stated state (issue #212)

A westward venue clock move carries bar stamps back with it, so `step_symbol`'s
advance gate holds for about the size of the move. That gate is #172's dump-trade
guard and it is UNCHANGED: no entry, no refusal, no halt, and nothing new reaching
the venue. What changed is that the hour is no longer indistinguishable from a
quiet market. The desk writes one `advance_held` row per symbol when that symbol's
stamps go backwards, carrying `behind_sec`: how far back they went, so also about
how much longer the gate holds.

**Once per symbol per backwards move, never one per poll.** At the default
`engine.poll_seconds` of 15 on H1 the gate returns with an unchanged stamp about
240 times an hour per symbol, about 960 across four, and `config.example.toml`
ships 1 second. A row per suppressed poll would bury the one row that means
something. Inside a held window the deficit only shrinks, so only a strictly
deeper deficit is a new move and earns its own row.

**It measures the stamps, not the recorded clock**, which is the one design call
worth reading. The venue clock is read in `_bar_instant`, reached through `_act`,
which the gate returns BEFORE, so the `venue_clock` row for a westward move lands
when the gate OPENS: a row conditioned on that record would fire after the hold
was over, or never. `prev - last_t` is already on the branch, it equals the move
at the first poll after the roll, and after that it is the better number.

**Not covered, and said rather than implied:** a venue that STALLS the top of its
series across the roll (the last bar keeps its old stamp) is identical to a quiet
market in everything that branch can see, so nothing is written for it.
`docs/RUNBOOK.md` carries the operator reading of the hour under `Live desk
limits` and the row's fields under `Journal`; `docs/CONTRACT.md` carries the
behaviour.
