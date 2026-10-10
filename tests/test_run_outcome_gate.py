"""Tests for the `main`-run surfacing gate.

The module carries its own `--self-test`, which is what CI runs as a visible
step. This file exists so the same cases run under `pytest` with the rest of the
suite, and so a future edit that loosens the comparison reds the suite rather
than only a workflow step somebody might delete.

The case that matters most is `cancelled`. Before the #329 concurrency fix, 18
of the 21 unverified commits on `main` were cancelled rather than failed, so a
gate keyed on "failure" would have missed almost all of them.
"""

from __future__ import annotations

import pytest

from run_outcome_gate import (
    SYNTHETIC_PREFIX,
    decide,
    render,
    self_test,
)


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "cancelled", "timed_out", "startup_failure", "stale", "action_required"],
)
def test_any_non_success_conclusion_alerts(conclusion: str) -> None:
    """The condition is the absence of success, not an enumeration of failures."""
    alert, reason = decide(conclusion, "main")
    assert alert is True
    assert conclusion in reason


def test_cancelled_alerts_because_it_is_the_common_case() -> None:
    """Named separately from the parametrised case so it cannot be dropped quietly."""
    assert decide("cancelled", "main")[0] is True


def test_success_is_silent() -> None:
    alert, reason = decide("success", "main")
    assert alert is False
    assert "verified" in reason


@pytest.mark.parametrize("conclusion", ["in_progress", "queued", ""])
def test_an_unfinished_run_is_not_an_alert(conclusion: str) -> None:
    """Registration lag is not a failure.

    An empty conclusion inside the first few seconds after a merge is the run
    not being registered yet. Treating it as a bad outcome would alert on every
    merge; treating it as `D=0` elsewhere would turn any outcome into a pass.
    """
    alert, reason = decide(conclusion, "main")
    assert alert is False
    assert "wiring" in reason


def test_another_branch_is_not_this_gates_business() -> None:
    assert decide("cancelled", "fix/something-123")[0] is False


def test_the_synthetic_marker_reaches_the_title() -> None:
    """A body-only marker is invisible in a notification list."""
    title, body = render("cancelled", "ci", "deadbeef", "http://x", synthetic=True)
    assert title.startswith(SYNTHETIC_PREFIX)
    assert "SYNTHETIC" in body


def test_a_real_alert_is_not_marked_synthetic() -> None:
    title, _ = render("cancelled", "ci", "deadbeef", "http://x", synthetic=False)
    assert not title.startswith(SYNTHETIC_PREFIX)


def test_synthetic_and_real_alerts_use_different_titles() -> None:
    """Different titles mean a synthetic test cannot update the real tracker."""
    syn, _ = render("cancelled", "ci", "deadbeef", "", synthetic=True)
    real, _ = render("cancelled", "ci", "deadbeef", "", synthetic=False)
    assert syn != real


def test_the_self_test_passes_on_the_shipped_logic() -> None:
    """Guards the guard: if `--self-test` ever stops passing, this reds too."""
    assert self_test() == 0
