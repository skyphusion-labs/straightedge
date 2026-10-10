"""What the real run would cost, as a CONSERVATIVE UPPER BOUND.

## Why a bound and not an exact count

The exact instrument is Anthropic's `POST /v1/messages/count_tokens`, which is
free and non-generative. It needs a credential, and this seat has none
(presence-checked with `${ANTHROPIC_API_KEY:+SET}`, which was empty), so the
figure here is computed from the fixtures' own character counts instead.

A bound is still decision grade, and that is the point of making it a CEILING
rather than an estimate: a vague figure is one whose direction of error is
unknown, while a ceiling with a stated method answers "is this cheap enough to
approve" without anyone needing the exact number. Every approximation below is
taken in the EXPENSIVE direction.

## The method, stated so it can be checked

INPUT. Measured in characters off the actual arm texts, then divided by
`CHARS_PER_TOKEN_FLOOR = 2.5`.

* The conventional English heuristic is about 4 characters per token.
* `tiktoken` is documented to undercount Claude tokens by 15 to 20 percent, so
  a Claude-corrected heuristic is about 4 / 1.2, roughly 3.33 characters per
  token.
* 2.5 is a further 1.33x above that corrected figure. The margin is deliberate:
  these snapshots are dense numeric and symbolic text (prices, tickets, symbol
  names), which tokenizes worse than prose, and a bound that needed the text to
  behave like prose would not be a bound.

OUTPUT. Charged at the FULL `max_tokens` ceiling the desk requests, which
`llm.py` sets to 8192 on the claude path. That is the real ceiling rather than
an expected length, and on current models it is the honest one: thinking is
always on and its tokens count against the same budget. The `grok` path sends
no `max_tokens` at all, so its output is not bounded by our request; it is
charged at the same 8192 here, which is a stand-in and is labelled as one.

A SENSITIVITY LINE is reported beside the ceiling using #165's own 500-token
output assumption, because the ceiling dominates the total and a reader should
be able to see by how much.

## Pricing

Claude rates are the published per-MTok figures carried by the `claude-api`
skill: `claude-opus-5-5` $4.00 in / $20.00 out, `claude-sonnet-5-5` $2.00 in /
$10.00 out.

There is NO published rate for `grok-4.6` in that skill, and this module does
not invent one. `total()` reports the two Claude providers exactly and reports
the grok column separately, priced as a clearly labelled stand-in at the most
expensive Claude rate. Substituting the real xAI rate is a one-line change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from straightedge.llm import SYSTEM

from advice_eval.arms import ARMS
from advice_eval.questions import QUESTIONS, Question
from advice_eval.runner import PROVIDERS, ProviderArm
from advice_eval.snapshots import Snapshot

#: Characters per token, taken LOW so the token count comes out HIGH. See the
#: module docstring for the derivation.
CHARS_PER_TOKEN_FLOOR = 2.5

#: `llm._claude` sets this ceiling, and thinking counts against it.
MAX_TOKENS_CEILING = 8192

#: #165's own output assumption, reported only as a sensitivity comparison.
ISSUE_OUTPUT_ASSUMPTION = 500


@dataclass(frozen=True)
class Rate:
    label: str
    input_per_mtok: float
    output_per_mtok: float
    #: True when the figure is a stand-in rather than that provider's published
    #: rate. Carried into the report so no reader mistakes one for the other.
    stand_in: bool = False


RATES: dict[str, Rate] = {
    "claude-opus-5-5": Rate("claude-opus-5-5", 4.00, 20.00),
    "claude-sonnet-5-5": Rate("claude-sonnet-5-5", 2.00, 10.00),
    # Priced at the dearest Claude rate purely so the matrix total has a
    # ceiling. NOT xAI's published price, which the claude-api skill does not
    # carry and which this harness will not guess.
    "xai/grok-4.6": Rate("xai/grok-4.6 (stand-in price)", 4.00, 20.00, stand_in=True),
}


def _ceil_tokens(chars: int) -> int:
    return int(-(-chars // CHARS_PER_TOKEN_FLOOR))


def request_text(snapshot: Snapshot, arm: str, question: Question) -> str:
    """Exactly what `Advisor.ask` would put on the wire for this cell.

    `ask` builds `user = f"{context}\\n\\nUser: {question}"` and the provider
    methods prepend `SYSTEM` with an EMPTY memory, because the harness uses a
    fresh advisor per call. So this is the whole billable input and not a
    sample of it.
    """
    user = f"{snapshot.arms[arm]}\n\nUser: {question.text}"
    return SYSTEM + "\n" + user


@dataclass
class Bound:
    calls: int
    input_tokens: int
    output_tokens_ceiling: int
    output_tokens_sensitivity: int

    def dollars(self, rate: Rate, *, ceiling: bool = True) -> float:
        out = self.output_tokens_ceiling if ceiling else self.output_tokens_sensitivity
        return (
            self.input_tokens / 1_000_000 * rate.input_per_mtok
            + out / 1_000_000 * rate.output_per_mtok
        )


def bound_for_one_provider(
    snapshots: Mapping[str, Snapshot],
    questions: tuple[Question, ...] = QUESTIONS,
) -> Bound:
    """Input and output bound for ONE provider across all arms and questions.

    Identical for every provider: the same arms and the same question list go to
    all three, which is #165's protocol. Tokenizers differ between vendors, but
    the character count does not, and the 2.5 floor is already generous enough
    to absorb that.
    """
    calls = 0
    input_tokens = 0
    for arm in ARMS:
        for question in questions:
            snapshot = snapshots[question.snapshot]
            input_tokens += _ceil_tokens(len(request_text(snapshot, arm, question)))
            calls += 1
    return Bound(
        calls=calls,
        input_tokens=input_tokens,
        output_tokens_ceiling=calls * MAX_TOKENS_CEILING,
        output_tokens_sensitivity=calls * ISSUE_OUTPUT_ASSUMPTION,
    )


def render(
    snapshots: Mapping[str, Snapshot],
    providers: tuple[ProviderArm, ...] = PROVIDERS,
    questions: tuple[Question, ...] = QUESTIONS,
) -> str:
    """The cost table, with its method stated in the output itself."""
    per_provider = bound_for_one_provider(snapshots, questions)
    lines = [
        "COST UPPER BOUND for the full matrix",
        f"method: input chars / {CHARS_PER_TOKEN_FLOOR} chars-per-token (conservative); "
        f"output charged at the full max_tokens ceiling of {MAX_TOKENS_CEILING}",
        f"arms={len(ARMS)} questions={len(questions)} providers={len(providers)}",
        f"calls per provider: {per_provider.calls}",
        f"input tokens per provider (upper bound): {per_provider.input_tokens}",
        f"output tokens per provider (ceiling): {per_provider.output_tokens_ceiling}",
        "",
        "provider\tupper bound\tsensitivity (500-token output)",
    ]
    total_ceiling = 0.0
    total_sensitivity = 0.0
    for provider_arm in providers:
        rate = RATES[provider_arm.label]
        ceiling = per_provider.dollars(rate, ceiling=True)
        sensitivity = per_provider.dollars(rate, ceiling=False)
        total_ceiling += ceiling
        total_sensitivity += sensitivity
        lines.append(f"{rate.label}\t${ceiling:.2f}\t${sensitivity:.2f}")
    lines.append("")
    lines.append(f"MATRIX TOTAL (upper bound): ${total_ceiling:.2f}")
    lines.append(f"MATRIX TOTAL (500-token-output sensitivity): ${total_sensitivity:.2f}")
    stand_ins = [r.label for r in RATES.values() if r.stand_in]
    if stand_ins:
        lines.append(
            "NOTE: these columns use a STAND-IN price, not a published rate: "
            + ", ".join(stand_ins)
        )
    lines.append(
        "NOTE: a figure this bound cannot tighten is the exact token count. "
        "Run count_tokens with a credential to replace the 2.5 chars-per-token "
        "floor with a measurement."
    )
    return "\n".join(lines)
