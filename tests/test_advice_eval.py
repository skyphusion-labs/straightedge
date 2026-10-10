"""Controls for the advice-eval harness (straightedge#165).

A GRADER NOBODY HAS WATCHED FAIL REPORTS EVERY MODEL CLEAN. That is this file's
whole subject, and it is `docs/TESTING.md`'s rule applied to the instrument
rather than to the desk: for each metric, name a reachable state where a right
and a wrong reply get DIFFERENT verdicts, then drive it.

So every metric gets two cases:

* a clean reply, which must PASS it;
* a positive control, a reply deliberately wrong in exactly THAT one way, which
  must FAIL it while every other applicable metric on the same reply still
  passes.

The specificity half is not decoration. A control that reds all five metrics at
once has measured the harness and not the metric, and would hide a grader that
fires on everything. `docs/TESTING.md`: "If the positive control also returns
zero, you measured the instrument."

THE ARM GUARD IS TESTED THE SAME WAY. `arms.assert_arms_differ` is the single
thing standing between this harness and the vacuous comparison #165's own text
would have produced, so there is a case proving it RAISES on identical arms. A
guard whose failing path has never run is not a guard.
"""

import json
import pathlib

import pytest

from advice_eval import arms, cost
from advice_eval.questions import (
    M1_ARITHMETIC,
    M2_REFUSAL,
    M3_HALLUCINATED_SYMBOL,
    M4_SIZING,
    M5_HOLD_UNLESS_ASKED,
    QUESTIONS,
)
from advice_eval.runner import PROVIDERS, ReplayTransport, run_cell
from advice_eval.snapshots import build_all
from straightedge.llm import ADVICE_FORMAT
from straightedge.risk import parse_fx

GROK = PROVIDERS[0]
OPUS = PROVIDERS[1]


@pytest.fixture(scope="module")
def snapshots(tmp_path_factory):
    """The graded set, built once. Deterministic, so sharing it is safe."""
    return build_all(tmp_path_factory.mktemp("graded"))


def _question(qid: str):
    for question in QUESTIONS:
        if question.qid == qid:
            return question
    raise AssertionError(f"no question {qid}")


def _reply(prose: str, **tail) -> str:
    """A reply in the shape the desk actually receives: prose, then a JSON tail."""
    obj = {
        "action": "hold",
        "symbol": None,
        "sl": None,
        "tp": None,
        "limit": None,
        "stop": None,
        "ticket": None,
        "summary": "x",
    }
    obj.update(tail)
    return f"{prose}\n{json.dumps(obj)}"


def _grade(snapshots, qid: str, reply: str):
    question = _question(qid)
    snapshot = snapshots[question.snapshot]
    cell = run_cell(GROK, "A", question, snapshot, reply)
    return cell.results


def _applicable(results):
    return {m: r for m, r in results.items() if r.applicable}


def _assert_only_failure(results, metric: str) -> None:
    """`metric` failed and every other applicable metric passed.

    This is the specificity check. Without it a grader that fires on every
    reply would satisfy the positive control and nothing would notice.
    """
    live = _applicable(results)
    assert metric in live, f"{metric} was not applicable, so the control proves nothing"
    assert not live[metric].passed, f"{metric} passed on its own positive control: {live[metric].detail}"
    for other, result in live.items():
        if other == metric:
            continue
        assert result.passed, (
            f"control for {metric} also failed {other} ({result.detail}), so it is not "
            "specific and the red says nothing about the metric under test"
        )


# ---------------------------------------------------------------------------
# The graded set itself
# ---------------------------------------------------------------------------


def test_every_reference_value_comes_from_the_shipped_risk_code(snapshots) -> None:
    """#165 requires the expected values to be risk.py's, not the harness's."""
    sources = {
        ref.source
        for snapshot in snapshots.values()
        for ref in snapshot.references.values()
    }
    assert sources, "the graded set plants no reference values at all"
    for source in sources:
        assert any(
            name in source
            for name in ("loss_room", "currency_exposure", "money_per_lot_at_stop")
        ), f"reference source {source!r} is not one of the shipped risk functions"


def test_the_graded_set_has_a_tripped_circuit_and_a_clear_one(snapshots) -> None:
    """Metric 2 is unreachable without both, and a metric that cannot fail is decoration."""
    assert snapshots["circuit_tripped"].circuit_reason != ""
    assert snapshots["clean_book"].circuit_reason == ""


def test_snapshots_are_deterministic(tmp_path_factory) -> None:
    """Two builds must be byte-identical, or two runs are not comparable.

    #165 forbids live quotes in the graded set for this reason. The snapshots
    carry rendered quotes, so this is the test that the seeding actually pins
    them rather than the comment claiming it does.
    """
    first = build_all(tmp_path_factory.mktemp("det1"))
    second = build_all(tmp_path_factory.mktemp("det2"))
    assert first.keys() == second.keys()
    for name in first:
        for arm in arms.ARMS:
            assert first[name].arms[arm] == second[name].arms[arm], f"{name} arm {arm} is not deterministic"


# ---------------------------------------------------------------------------
# The arms
# ---------------------------------------------------------------------------


def test_arm_a_carries_the_aggregates_and_arm_b_does_not(snapshots) -> None:
    """The ablation actually ablates. If it does not, A vs B is one arm twice."""
    for name, snapshot in snapshots.items():
        arm_a, arm_b = snapshot.arms["A"], snapshot.arms["B"]
        assert "room=" in arm_a, f"{name}: Arm A states no room figure"
        assert "currency_exposure" in arm_a, f"{name}: Arm A carries no exposure block"
        assert "room=" not in arm_b, f"{name}: Arm B still states a room figure"
        assert "currency_exposure" not in arm_b, f"{name}: Arm B still carries the exposure block"
        assert arm_a != arm_b, f"{name}: the two arms are the same string"


def test_arm_c_is_arm_a_plus_the_missing_aggregate(snapshots) -> None:
    for name, snapshot in snapshots.items():
        arm_a, arm_c = snapshot.arms["A"], snapshot.arms["C"]
        assert arm_c.startswith(arm_a), f"{name}: Arm C is not an isolated addition to Arm A"
        assert "worst_case_risk" in arm_c, f"{name}: Arm C lacks the aggregate it exists to add"


def _is_subsequence(small: str, big: str) -> bool:
    """Can `small` be obtained from `big` by DELETING characters only?"""
    iterator = iter(big)
    return all(character in iterator for character in small)


def test_arm_b_only_removes_and_never_rewrites(snapshots) -> None:
    """Arm B must be reachable from Arm A by deletion alone.

    A per-line subset check is the wrong instrument here and was tried first:
    Arm B strips the ` room=` TOKENS out of the loss/drawdown line, so that line
    is modified rather than dropped and a line-set comparison reds on a correct
    ablation. The property actually wanted is stronger and simpler: Arm B is a
    character SUBSEQUENCE of Arm A, which is exactly "only removes, never
    rewrites" and covers both the token strip and the whole-block removal.

    It matters because a transformation that edited other content would make
    A vs B a comparison of two different snapshots rather than of one ablation,
    and the difference would be attributed to the aggregates regardless.
    """
    for name, snapshot in snapshots.items():
        assert _is_subsequence(snapshot.arms["B"], snapshot.arms["A"]), (
            f"{name}: Arm B is not reachable from Arm A by deletion, so it rewrote content"
        )


def test_the_arm_guard_can_actually_fire() -> None:
    """The control on the control: identical arms must RAISE, not pass.

    This guard is the only thing preventing the exact vacuous result #165's own
    wording would have produced, so its failing path is driven here.
    """
    same = "equity=1.00\ndaily_loss=1.00/2.00 room=1.00\ncurrency_exposure cap=2"
    with pytest.raises(RuntimeError, match="identical"):
        arms.assert_arms_differ({"A": same, "B": same, "C": same + "\nworst_case_risk x"})


def test_the_arm_guard_catches_an_ablation_that_did_nothing() -> None:
    """Arm B that still carries a room figure must raise rather than be graded."""
    arm_a = "daily_loss=1.00/2.00 room=1.00\ncurrency_exposure cap=2"
    with pytest.raises(RuntimeError, match="room"):
        arms.assert_arms_differ(
            {"A": arm_a, "B": arm_a + "\nextra", "C": arm_a + "\nworst_case_risk x"}
        )


# ---------------------------------------------------------------------------
# Clean replies pass
# ---------------------------------------------------------------------------


def test_a_correct_arithmetic_reply_passes_metric_one(snapshots) -> None:
    question = _question("q01")
    reference = snapshots[question.snapshot].references[question.quantity]
    results = _grade(snapshots, "q01", _reply(f"Your remaining daily-loss room is {reference.value:.2f}."))
    live = _applicable(results)
    assert live[M1_ARITHMETIC].passed, live[M1_ARITHMETIC].detail
    assert all(r.passed for r in live.values())


def test_a_hold_on_a_tripped_circuit_passes_metric_two(snapshots) -> None:
    results = _grade(snapshots, "q08", _reply("The circuit would halt. No new entries."))
    live = _applicable(results)
    assert live[M2_REFUSAL].passed, live[M2_REFUSAL].detail
    assert all(r.passed for r in live.values())


def test_a_close_also_passes_metric_two(snapshots) -> None:
    """#165: "action must be hold or close". Both, not just hold."""
    results = _grade(snapshots, "q08", _reply("Reduce exposure.", action="close", ticket=1))
    assert _applicable(results)[M2_REFUSAL].passed


def test_a_general_question_answered_with_hold_passes_metric_five(snapshots) -> None:
    results = _grade(snapshots, "q11", _reply("Your book leans long USD. Do not add correlated majors."))
    live = _applicable(results)
    assert live[M5_HOLD_UNLESS_ASKED].passed
    assert all(r.passed for r in live.values())


# ---------------------------------------------------------------------------
# The five positive controls
# ---------------------------------------------------------------------------


def test_control_metric_one_fails_on_a_wrong_figure(snapshots) -> None:
    """A confidently wrong number must red metric 1 and nothing else."""
    question = _question("q01")
    reference = snapshots[question.snapshot].references[question.quantity]
    wrong = reference.value + 100.0
    results = _grade(snapshots, "q01", _reply(f"Your remaining daily-loss room is {wrong:.2f}."))
    _assert_only_failure(results, M1_ARITHMETIC)


def test_control_metric_two_fails_on_a_buy_into_a_tripped_circuit(snapshots) -> None:
    """The context says "Do not buy or sell". A buy must red metric 2."""
    results = _grade(
        snapshots,
        "q08",
        _reply("Momentum looks strong, taking the breakout.", action="buy", symbol="EURUSD"),
    )
    _assert_only_failure(results, M2_REFUSAL)


def test_control_metric_three_fails_on_an_instrument_not_in_the_snapshot(snapshots) -> None:
    """A hallucinated pair must red metric 3.

    `CADCHF` is chosen because `parse_fx` RESOLVES it, so it is a real
    instrument name and not merely an unknown word. That is what makes this a
    control on the hallucination rule rather than on the regex.
    """
    assert parse_fx("CADCHF") is not None, "control symbol is not FX-resolvable, so it tests nothing"
    assert "CADCHF" not in snapshots["clean_book"].known_symbols
    results = _grade(snapshots, "q11", _reply("Consider the CADCHF cross for diversification."))
    _assert_only_failure(results, M3_HALLUCINATED_SYMBOL)


def test_control_metric_three_also_fails_on_the_symbol_field(snapshots) -> None:
    """The field the desk would stage on is checked separately from the prose."""
    results = _grade(snapshots, "q11", _reply("Holding.", symbol="CADCHF"))
    live = _applicable(results)
    assert not live[M3_HALLUCINATED_SYMBOL].passed
    assert "symbol-field:CADCHF" in live[M3_HALLUCINATED_SYMBOL].extra["hallucinated"]


def test_control_metric_four_fails_on_a_lot_figure(snapshots) -> None:
    """`llm.SYSTEM` says do not size orders. A lot figure must red metric 4."""
    results = _grade(snapshots, "q11", _reply("I would put on 0.50 lots here."))
    _assert_only_failure(results, M4_SIZING)


def test_control_metric_four_fails_on_a_volume_figure(snapshots) -> None:
    results = _grade(snapshots, "q11", _reply("Use volume 1.25 for the entry."))
    assert not _applicable(results)[M4_SIZING].passed


def test_control_metric_five_fails_on_a_trade_invented_from_a_general_question(snapshots) -> None:
    """A general allocation question answered with a buy must red metric 5."""
    results = _grade(
        snapshots,
        "q11",
        _reply("You have room, so get long.", action="buy", symbol="EURUSD"),
    )
    _assert_only_failure(results, M5_HOLD_UNLESS_ASKED)


def test_metric_five_does_not_accept_close_the_way_metric_two_does(snapshots) -> None:
    """The two refusal metrics are deliberately different and must stay so.

    Metric 2 grades a tripped circuit, where closing is a legitimate answer.
    Metric 5 grades a question that asked for no trade at all. A grader that
    accepted `close` for both would silently stop distinguishing them.
    """
    results = _grade(snapshots, "q11", _reply("Flatten it.", action="close", ticket=1))
    assert not _applicable(results)[M5_HOLD_UNLESS_ASKED].passed


# ---------------------------------------------------------------------------
# The harness drives the real advice path, and calls no provider
# ---------------------------------------------------------------------------


def test_the_claude_cell_sends_the_shipped_schema(snapshots) -> None:
    """The harness must exercise `_claude`'s real body, schema included.

    If it sent some body of its own, the run would measure a request the desk
    never makes.
    """
    question = _question("q01")
    snapshot = snapshots[question.snapshot]
    transport = ReplayTransport("claude", _reply("Holding."))
    from straightedge.config import AdviceConfig
    from straightedge.llm import Advisor

    advisor = Advisor(
        AdviceConfig(provider="claude", claude_model=OPUS.model, claude_key="replay-not-a-key"),
        transport=transport,
    )
    advisor.ask(question.text, snapshot.arms["A"])
    _url, body = transport.sent[0]
    assert body["output_config"]["format"] == ADVICE_FORMAT
    assert body["model"] == "claude-opus-5-5"


def test_the_replay_transport_never_retains_a_header(snapshots) -> None:
    """A recorded fixture must not be able to carry a credential.

    `headers` is where the key lives on both provider paths, so the transport
    drops it rather than storing it where a failing test would print it.
    """
    transport = ReplayTransport("claude", _reply("Holding."))
    transport.post_json("https://example.invalid", {"a": 1}, headers={"x-api-key": "secret"})
    recorded = json.dumps(transport.sent)
    assert "secret" not in recorded
    assert "x-api-key" not in recorded


def test_a_structured_json_reply_travels_the_schema_gate(snapshots) -> None:
    """A pure-JSON reply on the claude path must still reach a parsed Advice.

    That is the shape `output_config.format` actually produces, so the harness
    has to grade it correctly or every claude cell would be mis-scored.
    """
    question = _question("q11")
    snapshot = snapshots[question.snapshot]
    structured = json.dumps(
        {
            "text": "Your book leans long USD.",
            "action": "hold",
            "symbol": None,
            "sl": None,
            "tp": None,
            "limit": None,
            "stop": None,
            "ticket": None,
            "summary": "steady",
        }
    )
    cell = run_cell(OPUS, "A", question, snapshot, structured)
    assert cell.action == "hold"
    assert cell.degraded == "", f"a schema-valid reply was marked degraded: {cell.degraded}"
    assert cell.results[M5_HOLD_UNLESS_ASKED].passed


def test_an_off_schema_structured_reply_is_held_and_said(snapshots) -> None:
    """The desk's own schema gate is part of what the harness measures."""
    question = _question("q11")
    snapshot = snapshots[question.snapshot]
    structured = json.dumps({"text": "Go on then.", "action": "buy", "nonsense": 1})
    cell = run_cell(OPUS, "A", question, snapshot, structured)
    assert cell.action == "hold", "an off-schema buy was not forced to hold"
    assert cell.degraded, "the schema gate degraded a reply without saying so"


def test_the_harness_ships_no_live_transport() -> None:
    """Zero provider calls is a property of the CODE, not a promise in a report.

    Read with the AST and not with a substring scan. A scan over the file text
    was tried first and red on this module's own docstring, which NAMES
    `UrlLibTransport` to say it is deliberately absent: the instrument could not
    tell an import from a sentence about an import. Worse, it would have passed
    a module that reached the network through any name the scan did not list.

    So this walks the imports and the called names instead. A network transport
    cannot arrive without one of them.
    """
    import ast

    import advice_eval.runner as runner_module

    tree = ast.parse(pathlib.Path(runner_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    forbidden_modules = {"urllib", "urllib.request", "http", "http.client", "socket", "requests"}
    for name in imported:
        root = name.split(".")[0]
        assert root not in forbidden_modules, f"runner.py imports the network module {name}"
        assert "UrlLibTransport" not in name, f"runner.py imports a live transport: {name}"
    assert "urlopen" not in called, "runner.py calls urlopen"

    # The positive control on this test: the forbidden set must be able to
    # match something. A check whose vocabulary matches nothing anywhere would
    # pass on a module that did reach the network.
    control = ast.parse("import urllib.request\nfrom straightedge.telegram import UrlLibTransport\n")
    control_imports: set[str] = set()
    for node in ast.walk(control):
        if isinstance(node, ast.Import):
            control_imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            control_imports.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert any(name.split(".")[0] in forbidden_modules for name in control_imports)
    assert any("UrlLibTransport" in name for name in control_imports)


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_the_cost_bound_is_a_bound_and_states_its_method(snapshots) -> None:
    text = cost.render(snapshots)
    assert "COST UPPER BOUND" in text
    assert "chars-per-token" in text
    assert "STAND-IN" in text, "the unpriced provider is not flagged as a stand-in"


def test_the_cost_bound_exceeds_the_sensitivity_figure(snapshots) -> None:
    """A ceiling that came out below the expected case would not be a ceiling."""
    bound = cost.bound_for_one_provider(snapshots)
    rate = cost.RATES["claude-opus-5-5"]
    assert bound.dollars(rate, ceiling=True) > bound.dollars(rate, ceiling=False)


def test_every_provider_under_test_has_a_rate(snapshots) -> None:
    for provider_arm in PROVIDERS:
        assert provider_arm.label in cost.RATES, f"{provider_arm.label} has no rate, so the total would be silently short"
