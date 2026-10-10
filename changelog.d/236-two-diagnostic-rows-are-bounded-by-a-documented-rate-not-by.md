### Two diagnostic rows are bounded by a documented RATE, not by the row constant (issue #236)

#226's row bound was never true of two of our own diagnostics:
`history_preflight` measured 876 bytes and `history_unavailable` 1108 on a
four-symbol book, against the 512 this PR proposed at the time, since raised to
580 and derived (#226). Neither carries model-chosen
content and both grow linearly with the operator's book, so the constant was
the wrong instrument rather than the rows being wrong.

**The rate, measured and now documented:** `history_preflight` at **177 bytes
per CONFIGURED symbol** (stable at 177.0 across books of 8, 20, 32 and 50) and
`history_unavailable` at **222 per UNUSABLE symbol**. The contract states 200
and 250 as the ceilings, which is the measurement plus room, and the test gates
them in both directions.

**The two populations are deliberately separate.** The first is the whole book
and is stable; the second is near zero on a healthy desk and spikes to the whole
book during exactly the incident somebody is reading the row to understand. One
rate for both would hide that.

**Summarising was considered and rejected on evidence.** The original filing
offered it as "detail for the first N symbols, a count for the rest", on the
assumption that the row repeated boilerplate. It does not: the remedy is written
once at the end and every other byte is a per-symbol fact (bars seen, bars
needed, attempts, whether the venue reported a history error, whether ATR could
be computed). A summary could only delete facts, and only during the incident.

**Both directions, which is the half an exemption never had.** Measured must be
at or under the documented rate, so a field added to the per-symbol entry fails
rather than quietly enlarging every row; and measured must be at least 70% of
it, so a rate set generously enough never to fail also fails, and a row that
STOPS carrying its per-symbol detail fails instead of passing a ceiling it no
longer approaches.

**The empty-reason hole is closed.** A review measured that a row exempted with
an EMPTY reason passed, so prose beside a name was discipline rather than a
gate. An entry now carries a population, a non-zero rate and a reason, all three
checked by one function that both the assertion and its control call, because a
control that re-implements the rule tests the control.

**And the population field was decorative until a mutation said so.** Declaring
the prose row against `configured` instead of `unusable` passed, because with
one symbol seeded the two counts differ by a constant and their slopes are
identical. Each row is now measured on a pair of books where only its OWN
population moves (8/1 to 24/17 for configured, 24/17 to 24/1 for unusable), so a
row declared against the wrong population is measured where it does not move and
fails the lower bound.

Five mutations, each red where it should be: the documented rate set below the
measurement reds 1, set generously above it reds 1, a blank reason reds 6
including the real path, the wrong population reds 1, and a 120 byte field added
to the per-symbol entry reds 1. Restored, 14 passed.
