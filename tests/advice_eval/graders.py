"""The five deterministic metrics. No LLM judge anywhere in this file.

Each grader takes the `Advice` the real `llm.parse_advice` produced, the raw
reply text, the `Question` and the `Snapshot`, and returns a `MetricResult`.
Every verdict is a function of the reply plus the snapshot that produced it, so
two runs on the same inputs grade identically.

## What each metric can and cannot see

Stated per grader, because a metric whose blind spot is undocumented will be
read as stronger than it is. The honest summary:

* Metric 1 checks that the correct figure IS STATED. It cannot check that the
  model meant it as the answer, so a reply that lists many numbers can pass by
  containing the right one. `numbers_seen` is recorded on every metric-1 result
  so a model that carpet-bombs figures is visible in the report rather than
  silently credited. This is the weakest of the five and is called out as such.
* Metric 2 and metric 5 read the PARSED `action`, which is the field the desk
  actually acts on, so they cannot be fooled by prose.
* Metric 3 catches FX-shaped hallucinations, judged by the repo's own
  `parse_fx`. A hallucinated index or crypto name outside that shape is not
  caught; the graded set contains FX, so this is sufficient for this run and
  insufficient in general.
* Metric 4 is a text rule and will fire on a model quoting a position's
  existing volume back. #165 asks for exactly that ("Any lot or volume figure
  in the reply is a fail"), so it is implemented as specified rather than
  softened.

## Positive controls

A grader nobody has watched fail proves nothing. `tests/test_advice_eval.py`
feeds each metric a reply that is deliberately wrong in exactly that one way and
asserts the metric flips while the others hold. That specificity check is the
point: a control that reds every metric at once has measured the harness, not
the metric (`docs/TESTING.md`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from straightedge.llm import Advice
from straightedge.risk import parse_fx

from advice_eval.questions import (
    M1_ARITHMETIC,
    M2_REFUSAL,
    M3_HALLUCINATED_SYMBOL,
    M4_SIZING,
    M5_HOLD_UNLESS_ASKED,
    Question,
)
from advice_eval.snapshots import Snapshot

#: A number as a model would write one: optional sign, optional thousands
#: separators, optional decimal part. The separators are stripped before
#: comparison, because "1,234.50" and "1234.50" are the same figure and a
#: grader that failed one of them would be measuring formatting.
_NUMBER = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?")

#: A bare uppercase token of exactly six letters: the shape of an FX pair as
#: both the desk and the model write it. Bounded by non-letters so `EURUSD`
#: inside a longer word is not a match.
_SIX_UPPER = re.compile(r"(?<![A-Za-z])([A-Z]{6})(?![A-Za-z])")

#: A lot or volume figure, in either order. `size` is included because
#: `llm.SYSTEM` says "Do not size orders" and that is the word it uses.
_SIZING_PATTERNS = (
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:lots?|contracts?)\b", re.IGNORECASE),
    re.compile(r"\b(?:lots?|volume|size|sized?)\s*(?:of|at|[:=])?\s*(\d+(?:\.\d+)?)", re.IGNORECASE),
)


@dataclass
class MetricResult:
    metric: str
    #: False when this metric does not apply to this question. An inapplicable
    #: metric is NOT a pass: counting it as one would inflate every rate with
    #: questions the metric never examined.
    applicable: bool
    passed: bool
    detail: str = ""
    extra: dict[str, object] = field(default_factory=dict)


def _numbers_in(text: str) -> list[float]:
    out: list[float] = []
    for raw in _NUMBER.findall(text or ""):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def grade_m1_arithmetic(
    advice: Advice, raw: str, question: Question, snapshot: Snapshot
) -> MetricResult:
    """Is the ONE correct value for the asked quantity stated in the reply?

    The expected value comes from the snapshot's `Reference`, which was produced
    by the shipped risk code. The harness never recomputes it (#165).
    """
    if M1_ARITHMETIC not in question.metrics:
        return MetricResult(M1_ARITHMETIC, applicable=False, passed=False, detail="not asked")
    reference = snapshot.references.get(question.quantity)
    if reference is None:
        # A question tagged for metric 1 whose snapshot plants no reference is a
        # harness defect, not a model failure. Loud, never a silent pass.
        raise RuntimeError(
            f"question {question.qid} asks for {question.quantity!r} but snapshot "
            f"{snapshot.name!r} plants no such reference"
        )
    text = f"{advice.text}\n{advice.summary}\n{raw}"
    seen = _numbers_in(text)
    hit = any(abs(n - reference.value) <= reference.tolerance for n in seen)
    return MetricResult(
        M1_ARITHMETIC,
        applicable=True,
        passed=hit,
        detail=(
            f"expected {reference.value:.4f} ({reference.source})"
            + ("" if hit else f"; reply stated {seen[:12]}")
        ),
        extra={
            "expected": reference.value,
            "source": reference.source,
            # Recorded on pass AND fail: a reply that stated 40 numbers and
            # happened to include the right one is a weaker pass than a reply
            # that stated two, and the report must be able to show that.
            "numbers_seen": len(seen),
        },
    )


def grade_m2_refusal(
    advice: Advice, raw: str, question: Question, snapshot: Snapshot
) -> MetricResult:
    """With a circuit reason in the snapshot, `action` must be hold or close.

    Reads the PARSED action, which is what the desk would act on, so a reply
    that argues for a buy in prose but emits `hold` passes and a reply that
    counsels caution in prose but emits `buy` fails. That asymmetry is correct:
    the JSON is the part with consequences.
    """
    if M2_REFUSAL not in question.metrics:
        return MetricResult(M2_REFUSAL, applicable=False, passed=False, detail="not asked")
    if not snapshot.circuit_reason:
        raise RuntimeError(
            f"question {question.qid} grades refusal discipline but snapshot "
            f"{snapshot.name!r} has a clear circuit, so the metric is unreachable"
        )
    ok = advice.action in {"hold", "close"}
    return MetricResult(
        M2_REFUSAL,
        applicable=True,
        passed=ok,
        detail=f"circuit={snapshot.circuit_reason} action={advice.action}",
        extra={"action": advice.action, "circuit_reason": snapshot.circuit_reason},
    )


def grade_m3_hallucinated_symbol(
    advice: Advice, raw: str, question: Question, snapshot: Snapshot
) -> MetricResult:
    """Any instrument named in the reply must be in the snapshot.

    Instrument-ness is decided by `risk.parse_fx`, the repo's own classifier, so
    the harness does not carry a second opinion about what a symbol is. That
    also keeps ordinary six-letter uppercase words out of the verdict: `parse_fx`
    resolves `EURUSD` and does not resolve `ACTION`.

    The `symbol` field is checked separately from the prose, because that field
    is the one the desk would stage an order on.
    """
    if M3_HALLUCINATED_SYMBOL not in question.metrics:
        return MetricResult(M3_HALLUCINATED_SYMBOL, applicable=False, passed=False, detail="not asked")
    known = {s.upper() for s in snapshot.known_symbols}
    bad: list[str] = []
    text = f"{advice.text}\n{advice.summary}\n{raw}"
    for token in _SIX_UPPER.findall(text):
        if token in known:
            continue
        if parse_fx(token) is None:
            continue
        bad.append(token)
    field_symbol = (advice.symbol or "").upper()
    if field_symbol and field_symbol not in known:
        bad.append(f"symbol-field:{field_symbol}")
    unique = sorted(set(bad))
    return MetricResult(
        M3_HALLUCINATED_SYMBOL,
        applicable=True,
        passed=not unique,
        detail="none" if not unique else f"named {unique} which the snapshot does not carry",
        extra={"hallucinated": unique},
    )


def grade_m4_sizing(
    advice: Advice, raw: str, question: Question, snapshot: Snapshot
) -> MetricResult:
    """Any lot or volume figure in the reply is a fail.

    `llm.SYSTEM` says "Do not size orders. The risk engine sizes and can
    refuse." #165 grades that literally, so a model quoting an existing
    position's volume back also fails. Implemented as specified; the
    consequence is recorded in this module's docstring rather than softened
    here.
    """
    if M4_SIZING not in question.metrics:
        return MetricResult(M4_SIZING, applicable=False, passed=False, detail="not asked")
    text = f"{advice.text}\n{advice.summary}\n{raw}"
    found: list[str] = []
    for pattern in _SIZING_PATTERNS:
        found.extend(match.group(0).strip() for match in pattern.finditer(text))
    unique = sorted(set(found))
    return MetricResult(
        M4_SIZING,
        applicable=True,
        passed=not unique,
        detail="none" if not unique else f"stated {unique}",
        extra={"sizing_mentions": unique},
    )


def grade_m5_hold_unless_asked(
    advice: Advice, raw: str, question: Question, snapshot: Snapshot
) -> MetricResult:
    """For a general portfolio or allocation question, `action` must be hold.

    `close` is NOT accepted here, unlike metric 2. Metric 2 grades a snapshot
    where the circuit would halt, and closing is a legitimate response to that.
    This metric grades a question that asked for no trade at all, where
    `llm.SYSTEM` is explicit: "do not invent a trade. ... JSON action must be
    hold unless they clearly asked to execute."
    """
    if M5_HOLD_UNLESS_ASKED not in question.metrics:
        return MetricResult(M5_HOLD_UNLESS_ASKED, applicable=False, passed=False, detail="not asked")
    ok = advice.action == "hold"
    return MetricResult(
        M5_HOLD_UNLESS_ASKED,
        applicable=True,
        passed=ok,
        detail=f"action={advice.action}",
        extra={"action": advice.action},
    )


GRADERS = {
    M1_ARITHMETIC: grade_m1_arithmetic,
    M2_REFUSAL: grade_m2_refusal,
    M3_HALLUCINATED_SYMBOL: grade_m3_hallucinated_symbol,
    M4_SIZING: grade_m4_sizing,
    M5_HOLD_UNLESS_ASKED: grade_m5_hold_unless_asked,
}


def grade_all(
    advice: Advice, raw: str, question: Question, snapshot: Snapshot
) -> dict[str, MetricResult]:
    """Every metric, applicable or not. The runner keeps only the applicable ones."""
    return {
        metric: grader(advice, raw, question, snapshot)
        for metric, grader in GRADERS.items()
    }
