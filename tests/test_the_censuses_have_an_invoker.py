"""Issue #263: both censuses detect drift, and nothing ran either of them.

`tests/double_census.py` (#244) and `tests/timing_census.py` (#262) are good
instruments and were dead letters: neither is a CI step, and neither is
collected by pytest, because neither is named `test_*`. So a new unmarked
elapsed-time test, or a new seam whose only doubles cannot fail, landed green
and joined the population silently -- which is the exact condition both
censuses were built because the HAND-KEPT version of their list had. The
derived list cannot be left to a person either.

WHY COLLECTED AS TESTS rather than a CI step, which is #263's option 3.

Riding the existing suite adds no required status check, and this repo has
`strict_required_status_checks_policy: true`, so every new required context is
paid again on every current of every other branch in the queue. The suite is
already a required context, so the censuses become gates here for free.

And it converts a report into an assertion. A census that prints a finding and
exits non-zero needs a reader; an assertion needs nobody. That is the same
print-versus-assert distinction as asserting on a journal record rather than
on the chat text.

I originally did NOT name them `test_*` on purpose, and the reason was worth
less than it looked: a census reads the suite, so collecting it inside the
suite felt self-referential. It is not a problem in practice -- these read
source text with `ast`, they do not import or execute the tests they describe,
so there is no ordering or state coupling to get wrong.

AND ONE CORRECTION TO MY OWN RECOMMENDATION, recorded because it was wrong in
a way worth keeping. I argued that `double_census` was NOT gateable because it
exits 1 today on three blind seams, and that gating it would red `main` on a
pre-existing finding. That was right about the exit code and wrong about the
only available shape: an EQUALITY pin on the known set is not an allowlist.
A silent ignore list is the #61 defect, where a scanner that skips what it
cannot resolve stops being a denominator. An asserted set is the opposite --
it fails in BOTH directions, so it cannot rot quietly, and it is the pattern
`tests/test_refusal_reasons.py` already uses with `UNREACHABLE` for the same
reason. The precedent was in the repo and I had not weighed it.
"""

from __future__ import annotations

import double_census
import timing_census

#: Seams that have doubles but no double that can FAIL, as measured today.
#:
#: PINNED, not allowlisted. Asserted for EQUALITY below, so this fails when a
#: new seam goes blind AND when one of these gains a failing double. Either way
#: a person edits this line deliberately and says why in the commit, which is
#: the whole difference from a list the tool skips quietly.
#:
#: These three are a RAISED FINDING, not an accepted state: each owes one of
#: the three answers in `docs/TESTING.md` (make a double enter the state,
#: disclaim the path, or cite a live run). Shrinking this set is the fix.
KNOWN_BLIND_SEAMS = frozenset({"cancel", "check_working", "working"})


def test_every_elapsed_time_assertion_is_marked_timing() -> None:
    """A new test that asserts on a wall-clock budget must say so.

    straightedge#258: the suite could not distinguish a test whose correctness
    requires a budget to be MET from one whose correctness is independent of
    timing, and ~15% of `windows-latest` runs went red on `main` with nothing
    wrong. The marks are durable because they live in the test files; this is
    what notices a new member.
    """
    rows = timing_census.collect()
    assert rows, (
        "the timing census found NO elapsed-time assertions anywhere, which "
        "means the instrument stopped working rather than the population "
        "being empty: there were 14 when this was written"
    )
    unmarked = sorted((f, fn) for f, fn, _, marked in rows if not marked)
    assert not unmarked, (
        "these tests assert on a wall-clock budget without saying so:\n  "
        + "\n  ".join(f"{f}::{fn}" for f, fn in unmarked)
        + "\n\nAdd @pytest.mark.timing, or make the assertion independent of "
        "timing the way #262 did for the ttl case. Run "
        "`python3 tests/timing_census.py` for the directions."
    )


def test_the_blind_seams_are_exactly_the_known_set() -> None:
    """A new seam whose doubles cannot fail must not land silently.

    straightedge#244: a double that only ever RETURNS cannot enter a failure
    state, so a suite built on it covers no failure path through that seam no
    matter how many cases it has.
    """
    implemented = double_census.collect()
    assert implemented, (
        "the double census found NO seam implemented by any test double, "
        "which means the instrument stopped working rather than the suite "
        "having no doubles: there were 14 seams when this was written"
    )
    blind = double_census.blind_seams(implemented)

    new = sorted(blind - KNOWN_BLIND_SEAMS)
    assert not new, (
        "these seams have doubles and NO double that can fail:\n  "
        + "\n  ".join(new)
        + "\n\nSo the suite covers no failure path through them. Make a double "
        "enter the state, disclaim the path in the test, or cite a live run. "
        "See docs/TESTING.md."
    )

    fixed = sorted(KNOWN_BLIND_SEAMS - blind)
    assert not fixed, (
        "these seams are no longer blind, which is good news this pin has to "
        "be told about:\n  "
        + "\n  ".join(fixed)
        + "\n\nRemove them from KNOWN_BLIND_SEAMS. The pin is asserted for "
        "equality on purpose, so progress updates it rather than hiding in it."
    )


def test_the_blind_seam_detector_can_tell_the_two_cases_apart() -> None:
    """Prove the detector discriminates, rather than trusting that it does.

    Both assertions above are only worth their green if `blind_seams` can
    actually distinguish a double that can raise from one that cannot. Fed
    both shapes directly, with no file reading involved.
    """
    cannot = {"seam": [("test_x.py", "Dummy", False)]}
    can = {"seam": [("test_x.py", "Dummy", True)]}
    mixed = {"seam": [("test_x.py", "A", False), ("test_x.py", "B", True)]}

    assert double_census.blind_seams(cannot) == {"seam"}
    assert double_census.blind_seams(can) == set()
    assert double_census.blind_seams(mixed) == set(), (
        "one double that can fail is enough to make the seam not blind"
    )
