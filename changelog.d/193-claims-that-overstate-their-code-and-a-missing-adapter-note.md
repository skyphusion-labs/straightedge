### Claims that overstate their code, and a missing adapter note (issue #193)

Residue from straightedge#182's approving review. Every item is a sentence, a
one-branch change or a test; no behaviour on the trading path moves.

- **`VENUE_CLOCK_BAR_DISAGREES` said it made a stale stamp "unreachable rather
  than merely unlikely". It NARROWS.** The comparison is against the forming
  bar's OPEN, so it sees a staleness only once that staleness exceeds the
  bar's AGE, which leaves it blind in the last moments before a bar closes:
  measured at a bar 899s old on a 900s series with the caller's bound violated,
  a 900s-stale stamp is accepted and the instant is wrong by 900s. The wording
  now says narrows, names the residual, and the boundary is pinned by test
  (`898` refuses, `899` does not) rather than left to the sentence.
- **`implied()` offered an offset outside the civil timezone band.** A clock
  frozen 48h on a UTC+3 server was reported as implying `UTC-45:00`, which is
  not a timezone, and the pre-#182 gate did say "outside the civil timezone
  band" at that staleness, so the display had lost the one reading that
  separated stale from absurd. Past the band `implied_offset_sec` is now
  absent and `doctor` says the stamp implies NO offset.
  **The band applies to the VALUE and not to the measurement state**, which is
  the part worth keeping: `venue_clock_check` keys its EXIT CODE on
  `unmeasured == {"freshness"}`, so refusing differently here would send
  doctor down the NOT MEASURED branch and exit non-zero past about 15h of
  staleness. That is a red `doctor` every weekend, which #182 decided against.
  A correction applied through the wrong seam re-creates the thing it was
  correcting, and a test pins the exit code at zero.
- **`measured_at` was carrying two meanings.** `declared()` left it zero and
  `Engine._bar_instant` gated its cross-check on `if clock.measured_at`, so a
  zero timestamp meant "nothing was sampled". That is what `__post_init__`
  already objects to for `offset_sec`. There is now a `sampled` flag for the
  gate, and the sentinel could not simply be dropped because `measured_at` is
  ALSO an operand in that check (`measured_at + offset_sec`), so
  `__post_init__` asserts that a sampled clock carries a real timestamp. The
  existing rule stated about a third field, not a new one: no current test or
  live path can produce the shape it rejects.
- **`Broker.venue_clock` is a contract change for a third-party adapter**, and
  this was missing from 1.7.0's entry. The method was added in 1.7.0 and takes
  a keyword-only `max_staleness_sec`. An adapter written against 1.6.0 has no
  such method and reads as NOT MEASURED through `venue_clock_of`, which
  refuses rather than assuming UTC; an adapter that added the 1.7.0 method
  without that argument now fails on the CALL rather than on the `getattr`.
  Either way the auto leg refuses every signal and `doctor --connect` is where
  it shows.
- **The uncertainty rule is conservative by design**, and said so as though it
  were exact. `2 * uncertainty` under one grid step is SUFFICIENT rather than
  necessary, because it treats a one-sided staleness as two-sided; it refuses
  some samples that could in principle be placed, and the direction of that
  error is a refusal rather than a wrong instant. Code unchanged.
