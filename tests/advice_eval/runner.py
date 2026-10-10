"""Drives the matrix: three providers, three arms, one question list.

THE TRANSPORT SEAM IS THE SPEND BOUNDARY. `Advisor` takes its transport by
injection, and the only transport this package ships is `ReplayTransport`.
There is no live transport in this harness, so it cannot call a provider even by
mistake. Making the matrix real is a one-file change that adds the repo's own
`telegram.UrlLibTransport`, and it is deliberately NOT made here: #165's Spend
section says the run needs Conrad's number first.

WHAT IS EXERCISED FOR REAL. Everything except the network. `Advisor.ask`
builds the provider body (including `_claude`'s `output_config.format` request),
and the reply comes back through `structured_to_parseable`, the schema gate and
`parse_advice`. So the harness grades the `Advice` the DESK would have acted on,
not a parse of its own.

FRESH ADVISOR PER CALL, and that is a design decision rather than tidiness.
`Advisor` keeps up to `llm.KEEP_TURNS` conversation turns, so a single advisor
walked through the question list would show question 14 a context shaped by
answers 1 to 13. Two arms would then no longer differ only in their aggregates,
and the comparison the harness exists for would be contaminated. Independent
calls also make the cost bound a simple multiple instead of a growing series.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from straightedge.config import AdviceConfig
from straightedge.llm import Advisor

from advice_eval.arms import ARMS
from advice_eval.graders import MetricResult, grade_all
from advice_eval.questions import METRICS, QUESTIONS, Question
from advice_eval.snapshots import Snapshot


@dataclass(frozen=True)
class ProviderArm:
    """One provider under test, named the way #165 names it."""

    label: str
    #: `AdviceConfig.provider`: "grok" or "claude".
    provider: str
    #: The model id sent on the wire.
    model: str


#: The three arms of the provider comparison (#165). The grok entry is the
#: current shipped default (`config.py:466`).
PROVIDERS: tuple[ProviderArm, ...] = (
    ProviderArm(label="xai/grok-4.6", provider="grok", model="grok-4.6"),
    ProviderArm(label="claude-opus-5-5", provider="claude", model="claude-opus-5-5"),
    ProviderArm(label="claude-sonnet-5-5", provider="claude", model="claude-sonnet-5-5"),
)


class ReplayTransport:
    """Replays a recorded or synthesized reply in the provider's own wire shape.

    Shapes the response the way `Advisor` expects to read it, so the reply
    travels the real code path: the `claude` branch reads `content[].text` and
    `stop_reason`, the `grok` branch reads `choices[0].message.content`.

    A FIXTURE MUST NEVER CARRY A CREDENTIAL. These are responses only. Nothing
    here records or replays a request header, and `sent` keeps the request
    BODIES so a test can assert what was asked for without touching auth.
    """

    def __init__(self, provider: str, reply: str) -> None:
        self.provider = provider
        self.reply = reply
        self.sent: list[tuple[str, dict]] = []

    def post_json(
        self, url: str, payload: dict, timeout: float = 10.0, headers: dict | None = None
    ) -> dict:
        # `headers` is accepted and DROPPED. It is where the credential lives,
        # and a transport that stored it would put a secret in whatever a test
        # later prints.
        self.sent.append((url, payload))
        if self.provider == "claude":
            return {"content": [{"type": "text", "text": self.reply}], "stop_reason": "end_turn"}
        return {"choices": [{"message": {"content": self.reply}}]}


def _advisor(provider_arm: ProviderArm, reply: str) -> tuple[Advisor, ReplayTransport]:
    transport = ReplayTransport(provider_arm.provider, reply)
    if provider_arm.provider == "claude":
        cfg = AdviceConfig(
            provider="claude",
            claude_model=provider_arm.model,
            # A NON-EMPTY PLACEHOLDER, never a real key. `AdviceConfig.enabled`
            # requires a truthy credential or `ask` short-circuits with the
            # "no AI key" message and nothing would be graded. The replay
            # transport never reads it.
            claude_key="replay-not-a-key",
        )
    else:
        cfg = AdviceConfig(
            provider="grok",
            grok_model=provider_arm.model,
            grok_key="replay-not-a-key",
        )
    # persist_path stays None: the harness must not write an advice memory file
    # beside anyone's journal.
    return Advisor(cfg, transport=transport), transport


@dataclass
class Cell:
    """One graded call: provider, arm, question."""

    provider: str
    arm: str
    qid: str
    snapshot: str
    action: str
    degraded: str
    results: dict[str, MetricResult]


@dataclass
class Report:
    cells: list[Cell] = field(default_factory=list)

    def rates(self) -> dict[tuple[str, str, str], tuple[int, int]]:
        """(provider, arm, metric) -> (passed, applicable).

        PER METRIC, PER MODEL, PER ARM, and never collapsed into one score.
        #165: "Report per-metric pass rates per model per arm, not a single
        score." A single score would let a model that fails refusal discipline
        average its way back to respectable on the four metrics that do not
        point at real money.
        """
        out: dict[tuple[str, str, str], tuple[int, int]] = {}
        for cell in self.cells:
            for metric, result in cell.results.items():
                if not result.applicable:
                    continue
                key = (cell.provider, cell.arm, metric)
                passed, total = out.get(key, (0, 0))
                out[key] = (passed + (1 if result.passed else 0), total + 1)
        return out

    def render(self) -> str:
        lines = ["provider\tarm\tmetric\tpassed/applicable\trate"]
        rates = self.rates()
        for provider_arm in PROVIDERS:
            for arm in ARMS:
                for metric in METRICS:
                    key = (provider_arm.label, arm, metric)
                    if key not in rates:
                        continue
                    passed, total = rates[key]
                    pct = (100.0 * passed / total) if total else 0.0
                    lines.append(
                        f"{provider_arm.label}\t{arm}\t{metric}\t{passed}/{total}\t{pct:.1f}%"
                    )
        return "\n".join(lines)


ReplyFor = Mapping[tuple[str, str, str], str]


def run_cell(
    provider_arm: ProviderArm,
    arm: str,
    question: Question,
    snapshot: Snapshot,
    reply: str,
) -> Cell:
    """One call through the real advice path, graded."""
    advisor, _transport = _advisor(provider_arm, reply)
    advice = advisor.ask(question.text, snapshot.arms[arm])
    results = grade_all(advice, reply, question, snapshot)
    return Cell(
        provider=provider_arm.label,
        arm=arm,
        qid=question.qid,
        snapshot=snapshot.name,
        action=advice.action,
        degraded=advice.degraded,
        results=results,
    )


def run_matrix(
    snapshots: Mapping[str, Snapshot],
    replies: ReplyFor,
    providers: tuple[ProviderArm, ...] = PROVIDERS,
    questions: tuple[Question, ...] = QUESTIONS,
) -> Report:
    """The full matrix. `replies` is keyed (provider label, arm, qid).

    A missing reply RAISES rather than being skipped. A skipped cell would
    shrink the denominator silently, and a rate over an unknown denominator is
    the shape of result this harness exists to avoid.
    """
    report = Report()
    for provider_arm in providers:
        for arm in ARMS:
            for question in questions:
                snapshot = snapshots[question.snapshot]
                key = (provider_arm.label, arm, question.qid)
                if key not in replies:
                    raise RuntimeError(f"no reply recorded for {key}")
                report.cells.append(
                    run_cell(provider_arm, arm, question, snapshot, replies[key])
                )
    return report
