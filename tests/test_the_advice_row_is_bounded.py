"""straightedge#226: the `advice_turn` row was unbounded through `symbol`.

#216 bounded the degrade REASON and this is the same exposure one field over.
`symbol` is model-chosen on the advice path, which is the whole premise of
#197, and the row wrote it verbatim:

    5600-char ASCII symbol      ->  5838-byte row, `degraded` EMPTY
    3000-char non-ASCII symbol  ->  5754-byte row

**And #216's own `test_the_row_stays_bounded_and_carries_no_prose` passes while
that row is reachable**, because it varies `text` and `summary`, neither of
which is echoed. That is the second time a bound on this row was asserted by
FIXTURE rather than by construction, and the first was the defect #216 exists
to fix: my own pattern, recurring one field over.

So the fix is not "bound `symbol` too". It is to drive every model-reachable
field large AT ONCE and require the whole row under the documented bound, with
the field list derived from the code's own property table rather than from a
list someone maintains by hand. A field added to `ADVICE_PROPERTIES` tomorrow
is driven large by these tests without anyone remembering to add it.

## Two defects, measured

`symbol`: unbounded, above. `ticket`: `_int` did `int(_num(v))`, so

* `1e308` produced a **309 digit** integer and a 501 byte row on its own, which
  alone nearly breaches the bound;
* a 400 digit ticket string raised **`OverflowError`**, which derives from
  `ArithmeticError` and is therefore NOT in the
  `(ValueError, RuntimeError, OSError)` tuple `handle_command` catches, so it
  was an uncaught exception out of the command handler. Same family #219 found
  in `normalize_volume`, reached through a model reply instead of a command.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import Engine
from straightedge.journal import (
    RECORD_ROW_BOUND,
    RECORD_STRING_CHARS,
    clip_for_record,
)
from straightedge.llm import ADVICE_PROPERTIES, Advisor, parse_advice
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand

#: Taken from the code, not repeated here, so the documented bound and the
#: asserted bound cannot drift (straightedge#226).
ROW_BOUND = RECORD_ROW_BOUND


class FakeTransport:
    def __init__(self, *payloads: dict) -> None:
        self.payloads = list(payloads)

    def post_json(self, url: str, payload: dict, timeout: float = 10.0, headers=None):
        del timeout, headers
        if url.endswith("/getUpdates"):
            return {"ok": True, "result": []}
        if url.endswith("/sendMessage"):
            return {"ok": True, "result": {"message_id": 1}}
        return self.payloads.pop(0) if self.payloads else {}


def _claude_reply(obj: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj)}]}


#: NOTE the default here is `claude` and the SHIPPED default is `grok`
#: (`AdviceConfig.provider`, `config.example.toml`, and the `AI_PROVIDER`
#: fallback all say `grok`). This is a test-file convenience, because most cases
#: in this file want the structured path. It has already misled one reader into
#: filing straightedge#284 on the premise that `provider="grok"` was a departure
#: from the default when it is the shipped value, so it is written down rather
#: than left to be re-derived.
def _engine(tmp_path: Path, *payloads: dict, provider: str = "claude") -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.advice.provider = provider
    cfg.advice.claude_key = "k"
    cfg.advice.grok_key = "k"
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    advisor = Advisor(
        cfg.advice, transport=FakeTransport(*payloads), persist_path=tmp_path / "a.json"
    )
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), advisor=advisor)
    engine.start()
    return engine


def _turn(engine: Engine) -> dict:
    rows = [r for r in engine.journal.tail(5000) if r.get("event") == "advice_turn"]
    assert rows, "no advice turn was journalled"
    return rows[-1]


@dataclass(frozen=True)
class GrowthRate:
    """What an exempt diagnostic row's size is a function OF, and how steeply.

    #236's ruling: a diagnostic gets a documented growth RATE rather than the
    512 byte constant, because its size is a property of the operator's book
    rather than of anything a model can say. A constant would be a lie and an
    exemption with prose beside it is discipline rather than a gate: a review
    measured that a row exempted with an EMPTY reason passed, so the entry
    carries numbers that are checked instead of a sentence that is not.

    `population` names WHICH count the row scales with, and the two are not the
    same question. `history_preflight` grows with every CONFIGURED symbol, so
    it is a property of the operator's book and it is stable. The prose row
    grows with the UNUSABLE ones, which is near zero on a healthy desk and
    spikes to the whole book during exactly the incident somebody is reading
    the row to understand. Documenting one rate for both would hide that.
    """

    population: str
    bytes_per_item: int
    why: str


#: Rows that DO exceed the bound and carry no model-chosen content, each with
#: what it scales with, so the exemption is a decision on the record rather
#: than a hole. Measured on this fixture's four-symbol book:
#: `history_preflight` 876 bytes, `history_unavailable` 1108. Both are our own
#: diagnostics, both grow with the OPERATOR's symbol book rather than with
#: anything a model can say, and neither is reachable by a reply. Whether they
#: should be bounded or summarised is a separate question about operator
#: diagnostics, filed rather than decided here (straightedge#236).
#:
#: A row that exceeds the bound and is NOT in this map fails, so a new row on
#: the advice path has to be classified by a person.
ROWS_EXEMPT_FROM_THE_BOUND = {
    "history_preflight": GrowthRate(
        population="configured",
        bytes_per_item=200,
        why="one entry per configured symbol: bars, needed, attempts, status, "
        "history_error, ATR",
    ),
    "history_unavailable": GrowthRate(
        population="unusable",
        bytes_per_item=250,
        why="our own prose, a paragraph per unusable symbol",
    ),
}


def bad_exemptions(entries: dict[str, GrowthRate]) -> list[str]:
    """Every exemption entry that does not state a checkable rate, and why.

    ONE checker, called by `_assert_every_row_in_bound` and by the control
    below, because a control that re-implements the rule proves the control
    rather than the rule. #236's ruling named this hole: a row exempted with an
    EMPTY reason passed, so prose beside a name was discipline and not a gate.
    """
    out: list[str] = []
    for name, rate in entries.items():
        if rate.population not in POPULATIONS:
            out.append(f"{name}: population {rate.population!r} is not measured")
        if rate.bytes_per_item <= 0:
            out.append(f"{name}: no growth rate")
        if not rate.why.strip():
            out.append(f"{name}: no reason")
    return out


def _assert_every_row_in_bound(engine: Engine) -> None:
    """EVERY row the turn wrote, not only `advice_turn`.

    One model reply writes several rows. The same large-symbol reply that
    produced the headline 5838 byte `advice_turn` row ALSO writes a `reject`
    row carrying the symbol, and a review measured that removing the clip from
    the reject site alone left a 5753 byte row with the whole suite green,
    because every test here read one event name. A bound asserted on the row
    the author was thinking about is the fixture-shaped assertion this issue is
    about, one event over.

    So the assertion is over the journal rather than over a chosen row: a new
    row added to the advice path tomorrow is covered without anyone editing
    this file, exactly as a new schema field is.
    """
    rows = list(engine.journal.tail(5000))
    oversized = [
        (row.get("event"), len(json.dumps(row, sort_keys=True)))
        for row in rows
        if len(json.dumps(row, sort_keys=True)) > ROW_BOUND
        and row.get("event") not in ROWS_EXEMPT_FROM_THE_BOUND
    ]
    assert not oversized, (
        f"{len(oversized)} journal row(s) exceed the {ROW_BOUND} byte bound and "
        f"are not classified in ROWS_EXEMPT_FROM_THE_BOUND: {oversized!r}. "
        "Either the row carries model-chosen content and must be clipped, or "
        "it is a diagnostic that scales with something the operator set and "
        "needs a line in that map saying so."
    )

    # AND THE EXEMPTION IS NOT A BLANK CHEQUE. An exempt row that starts
    # carrying model text is the exact defect this file exists for, so the
    # marker strings the fixture drives are looked for in those rows too.
    for row in rows:
        if row.get("event") not in ROWS_EXEMPT_FROM_THE_BOUND:
            continue
        blob = json.dumps(row, sort_keys=True)
        for marker in ("E" * 50, "Z" * 50, "9" * 50):
            assert marker not in blob, (
                f"the exempt row {row.get('event')!r} is carrying model-chosen "
                "text, so its exemption no longer holds"
            )

    # EVERY EXEMPTION CARRIES NUMBERS THAT ARE CHECKED. A review measured that
    # an entry with an EMPTY reason passed, so prose beside a name was never a
    # gate. The rate itself is asserted in both directions by
    # `test_the_documented_growth_rate_is_the_measured_one`; this is the
    # cheaper half, that nothing can be exempted without stating what it scales
    # with and how steeply.
    broken = bad_exemptions(ROWS_EXEMPT_FROM_THE_BOUND)
    assert not broken, (
        "an exemption from the row bound does not state what it scales with: "
        f"{broken!r}"
    )

    # And a name in the map that no longer appears is a stale exemption, which
    # would quietly widen the hole as rows are renamed.
    seen = {row.get("event") for row in rows}
    stale = sorted(set(ROWS_EXEMPT_FROM_THE_BOUND) - seen)
    assert not stale, (
        f"ROWS_EXEMPT_FROM_THE_BOUND names {stale!r}, which this turn did not "
        "write; a stale exemption widens the bound for nothing"
    )


def _rows_named(engine: Engine, event: str) -> list[dict]:
    """Every row of one event name, so a test can prove it reached a path."""
    return [r for r in engine.journal.tail(5000) if r.get("event") == event]


def _everything_large() -> dict:
    """A reply with EVERY schema field driven large, derived from the code.

    `ADVICE_PROPERTIES` is the table the request is built from and the one
    `_schema_violations` validates against, so a field added there is driven
    large here without anyone editing this fixture. That is the difference
    between a bound asserted by construction and one asserted by whichever
    fields the author happened to think of.
    """
    obj: dict[str, object] = {}
    for name, spec in ADVICE_PROPERTIES.items():
        types = spec.get("type")
        types = types if isinstance(types, list) else [types]
        if "string" in types:
            obj[name] = "E" * 6000
        elif "number" in types:
            obj[name] = "9" * 5000
        elif "integer" in types:
            obj[name] = "9" * 400
        else:
            obj[name] = "X" * 6000
    # Plus a model-named key, which is the channel #216 closed, so this fixture
    # drives every known field AND the unknown-field path at once.
    obj["Z" * 4000] = 1
    return obj


# --- 1. the bound, by construction -----------------------------------------


def test_every_model_reachable_field_driven_large_at_once_stays_in_bound(
    tmp_path: Path,
) -> None:
    """The case #216's version could not see. Red before this change.

    Every field of the schema large, plus a 4000 character model-named key, in
    one reply. The row has to stay under the documented bound, and no field may
    carry a model string whole.
    """
    engine = _engine(tmp_path, _claude_reply(_everything_large()))
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    row = _turn(engine)
    blob = json.dumps(row, sort_keys=True)
    assert len(blob) <= ROW_BOUND, (
        f"the row is {len(blob)} bytes with every field driven large: {blob[:200]}"
    )
    assert "EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE" not in blob, (
        "a model string reached the row whole"
    )
    assert "ZZZZ" not in blob
    _assert_every_row_in_bound(engine)
    engine.stop()


def test_the_same_reply_through_the_bare_parser_is_also_in_bound(
    tmp_path: Path,
) -> None:
    """`grok` has no schema gate, so the parser is the only thing in front.

    The claude path forces `hold` on a schema violation, which could mask the
    bound by blanking fields; this path does not, so the row carries the
    parser's own output and is the harder case.
    """
    tail = {k: v for k, v in _everything_large().items() if k in ADVICE_PROPERTIES}
    tail["action"] = "buy"
    engine = _engine(
        tmp_path,
        {"choices": [{"message": {"content": "prose\n" + json.dumps(tail)}}]},
        provider="grok",
    )
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    blob = json.dumps(_turn(engine), sort_keys=True)
    assert len(blob) <= ROW_BOUND, f"{len(blob)} bytes: {blob[:200]}"
    _assert_every_row_in_bound(engine)
    engine.stop()


#: The vantages the close case is driven on. Declared once because the pin
#: below reads it: a second hand-written copy in the parametrize list is the
#: drift this file already warns about elsewhere.
PROVIDERS_UNDER_TEST = ("claude", "grok")


#: The close reply, in whichever envelope the provider under test reads.
#:
#: ONE payload, two envelopes, because the thing being proven is that the path
#: is not provider-specific. Two hand-built payloads could drift and the test
#: would still pass, which is the "consumer that rebuilds what it should call"
#: shape `docs/TESTING.md` warns about.
#:
#: THE `text` KEY ON THE CLAUDE ARM IS LOAD-BEARING. `text` is `required` in
#: `ADVICE_FORMAT`, so a claude payload without it is a SCHEMA VIOLATION
#: (`_schema_violations` returns `[("text", "missing")]`), `action` is forced to
#: `hold`, the close never stages, and NO `reject` row is written at all. Drop
#: it and this case stops measuring anything on that arm. The grok and computer
#: paths have no schema gate, so they never noticed it was absent.
def _close_the_model_asked_for(provider: str) -> dict:
    tail = {
        "action": "close", "symbol": "E" * 5600, "ticket": None,
        "sl": None, "tp": None, "limit": None, "stop": None, "summary": "s",
    }
    if provider == "claude":
        return _claude_reply(dict(tail, text="p"))
    return {"choices": [{"message": {"content": "p\n" + json.dumps(tail)}}]}


@pytest.mark.parametrize("provider", PROVIDERS_UNDER_TEST)
def test_a_close_the_model_asked_for_bounds_every_row_it_writes(
    tmp_path: Path, provider: str
) -> None:
    """The PATH no field derivation could reach, found in review.

    `_stage_close` wrote three `reject` rows carrying `advice.symbol` raw, and
    a close is NEVER gated by `advice_allows`, so that symbol is model-chosen
    with nothing in front of it. `ADVICE_PROPERTIES["symbol"]` carries no
    `maxLength`, so a 5600 character symbol is a valid string: no violation, no
    forced hold, and this is the DEFAULT shape rather than a `grok`-only
    vantage.

    Measured before the fix: `reject reason=close_needs_ticket` at **5751
    bytes** with the whole symbol on it, while the `advice_turn` row beside it
    was 299 bytes and reported nothing wrong. The row this file was reading was
    clean and the row next to it was the defect, which is the same shape as
    #216 bounding a field and this PR first bounding one row.

    Driving `action = "close"` with no ticket is what reaches it, and the fix
    is at `_reject` rather than at the three call sites, because that is the
    single writer of every `reject` row and a fourth site would otherwise
    repeat this.

    PARAMETERISED OVER BOTH PROVIDERS (straightedge#284), because the sentence
    above claims the vantage is not `grok`-only and only `grok` was driven. A
    docstring was doing the fixture's job.

    TWO THINGS THE ISSUE ASSUMED THAT MEASUREMENT CONTRADICTS, recorded because
    both would have produced a worse test.

    **`grok` IS the product default**, so the original case was already driving
    it: `AdviceConfig.provider` defaults to `"grok"` (`config.py`),
    `config.example.toml` ships `provider = "grok"`, and the `AI_PROVIDER`
    fallback is `"grok"`. What reads as a departure from the default is
    `_engine`'s OWN default of `"claude"`, which is a test-file convenience and
    not the shipped shape. The claim worth proving was never "drive the
    default", it was "this is not provider-specific".

    **And the claude arm does NOT need different assertions.** The issue
    expected the schema gate to force `hold` here, blank the symbol and write no
    `reject` row, which would have meant two cases. Measured: that happens only
    when the fixture omits `text`, a `required` property, so the hold was an
    artifact of an INCOMPLETE PAYLOAD rather than a property of the path. With
    the payload schema-complete, both arms write one `reject` row, both read
    `close_needs_ticket`, both carry the identical clipped symbol, and both rows
    measure 212 bytes. Encoding that difference as two cases would have frozen a
    fixture bug into the suite as if it were behaviour.
    """
    engine = _engine(tmp_path, _close_the_model_asked_for(provider), provider=provider)
    engine.handle_command(TgCommand("1", 1, "/ask flatten it", 1))

    turn = _turn(engine)
    assert turn.get("action") == "close", (
        "the turn did not reach the close path on provider "
        + provider
        + f", so nothing below measures the reject row: action={turn.get('action')!r} "
        + f"violations={turn.get('violations')!r}. On the claude arm the usual "
        "cause is a payload missing a `required` schema property, which forces "
        "action to hold; see _close_the_model_asked_for."
    )

    rejects = _rows_named(engine, "reject")
    assert rejects, "the close never reached the reject path, so this proves nothing"
    assert rejects[-1]["reason"] == "close_needs_ticket", (
        f"not the refusal this case is about: {rejects[-1]!r}"
    )
    assert "[+5552 chars]" in rejects[-1]["symbol"], (
        "the reject row does not say what it dropped: "
        + str(rejects[-1]["symbol"])[:80]
    )
    _assert_every_row_in_bound(engine)
    engine.stop()


def test_the_close_case_covers_the_SHIPPED_default_provider() -> None:
    """The docstring above says `grok` is the default. Assert it, do not say it.

    straightedge#284 was filed believing the case did not drive the default
    provider, and the belief was reasonable: `_engine` defaults to `"claude"`,
    so `provider="grok"` reads as an explicit departure from the default rather
    than as the shipped value. It is the test file's convenience that differs
    from the product, not the case.

    Leaving that in prose is the exact defect #284 fixed one level up, where a
    docstring asserted a vantage the fixture never drove. So the shipped default
    is read from the config here, and if it ever moves to a provider this case
    does not drive, this reds and names it.
    """
    shipped = BotConfig().advice.provider
    assert shipped in PROVIDERS_UNDER_TEST, (
        f"the shipped default provider is {shipped!r}, which this case does not "
        f"drive (it drives {list(PROVIDERS_UNDER_TEST)}), so the bound on the "
        "close path is unproven on the configuration customers actually run. "
        "Add it to PROVIDERS_UNDER_TEST, with its reply envelope in "
        "_close_the_model_asked_for."
    )


def test_the_signal_path_row_is_bounded_too(tmp_path: Path) -> None:
    """The OTHER branch of the same writer, and it is reachable by an operator.

    `_reject` fills `symbol` from a `Signal` when it has one, and that branch
    was unclipped as well. FOUND BY MUTATION: removing the clip there left the
    whole file green, so it was a second unpinned branch beside the one the
    review found, and labelling it an unreachable backstop would have been
    wrong because it is not unreachable.

    Measured: `/buy <5600 chars> sl=0.9 tp=1.3` with `min_rr` high refuses with
    `rr_below_min` and writes a `reject` row through the signal branch. The
    operator typed that symbol rather than a model choosing it, which changes
    who to blame and changes nothing about the row: the bound exists because
    the row is machine-read, and a paste can breach it as easily as a reply
    can.
    """
    engine = _engine(tmp_path)
    engine.cfg.risk.min_rr = 99.0
    reply = engine.handle_command(
        TgCommand("1", 1, "/buy " + "E" * 5600 + " sl=0.9 tp=1.3", 1)
    )
    assert reply == "refused: rr_below_min", f"not the refusal this case needs: {reply!r}"

    rejects = _rows_named(engine, "reject")
    assert rejects, "the signal path wrote no reject row, so this proves nothing"
    assert rejects[-1]["reason"] == "rr_below_min", rejects[-1]
    assert "[+5552 chars]" in rejects[-1]["symbol"], (
        "the signal-path row does not say what it dropped: "
        + str(rejects[-1]["symbol"])[:80]
    )
    _assert_every_row_in_bound(engine)
    engine.stop()


def test_an_operator_typed_symbol_is_never_reshaped_on_a_reject_row(
    tmp_path: Path,
) -> None:
    """THE CONTROL for clipping at the writer rather than at one caller.

    `_reject` is reached by the telegram and auto legs too, where the symbol is
    operator-configured and must come back whole. A clip at the single writer
    is only safe if that is true, so it is asserted rather than assumed.
    """
    engine = _engine(tmp_path)
    engine.cfg.risk.min_rr = 99.0
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    rejects = _rows_named(engine, "reject")
    assert rejects, "the operator command did not refuse, so nothing is on the record"
    assert rejects[-1]["symbol"] == "EURUSD", (
        f"an operator-typed symbol was reshaped: {rejects[-1]['symbol']!r}"
    )
    engine.stop()


# --- 2. the two fields that were unbounded, each on its own ----------------


def test_a_long_symbol_is_clipped_and_the_row_says_so(tmp_path: Path) -> None:
    """The #226 case as filed: 5600 characters, which gave a 5838 byte row.

    The marker matters as much as the bound. A silently truncated value reads
    as the whole value, so a reader could not tell `EURUSD` from a 5600
    character string beginning with it.
    """
    engine = _engine(
        tmp_path,
        {"choices": [{"message": {"content": "p\n" + json.dumps({
            "action": "buy", "symbol": "E" * 5600, "sl": 1.0, "tp": 1.2,
            "limit": None, "stop": None, "ticket": None, "summary": "s",
        })}}]},
        provider="grok",
    )
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    row = _turn(engine)
    assert len(json.dumps(row)) <= ROW_BOUND
    assert "[+5552 chars]" in row["symbol"], (
        "the row does not say what it dropped: " + row["symbol"][:80]
    )

    # THE SAME REPLY WROTE A REJECT ROW. `E` * 5600 is not an allowed symbol,
    # so the desk also journals `symbol_not_allowed` with the symbol on it, and
    # that row was unpinned: measured at 5753 bytes with the reject clip
    # removed while this test still passed, because it read `advice_turn` only.
    rejects = _rows_named(engine, "reject")
    assert rejects, "the reply did not reach the reject path, so this proves nothing"
    assert rejects[-1].get("reason") == "symbol_not_allowed", (
        f"not the reject this case is about: {rejects[-1]!r}"
    )
    assert "[+5552 chars]" in rejects[-1]["symbol"], (
        "the reject row does not say what it dropped: "
        + str(rejects[-1]["symbol"])[:80]
    )
    _assert_every_row_in_bound(engine)
    engine.stop()


def test_a_huge_ticket_is_not_a_ticket(tmp_path: Path) -> None:
    """`1e308` gave a 309 digit integer and a 501 byte row on its own.

    A ticket is a venue handle, so a value no venue could have issued is not a
    ticket. Refusing it is the same rule as `_num` returning None for a
    non-number, and it keeps the digits of an exponent out of the record.
    """
    advice = parse_advice(
        "p\n" + json.dumps({"action": "buy", "symbol": "EURUSD", "ticket": 1e308})
    )
    assert advice.ticket is None
    assert parse_advice('p\n{"action": "buy", "ticket": 9223372036854775000}').ticket
    del tmp_path


def test_a_400_digit_ticket_does_not_raise_out_of_the_handler(
    tmp_path: Path,
) -> None:
    """`int(inf)` raises `OverflowError`, which the handler does NOT catch.

    `OverflowError` derives from `ArithmeticError`, so it is outside the
    `(ValueError, RuntimeError, OSError)` tuple `Desk.handle_command` catches:
    before this it left the handler as an uncaught exception. Driven through
    the command path rather than the parser, because that is where the escape
    mattered.
    """
    engine = _engine(
        tmp_path,
        {"choices": [{"message": {"content": "p\n" + json.dumps({
            "action": "buy", "symbol": "EURUSD", "ticket": "9" * 400,
        })}}]},
        provider="grok",
    )
    reply = engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    assert reply, "the handler returned nothing, so something escaped"
    assert _turn(engine)["ticket"] is None
    engine.stop()


#: The two counts an exempt diagnostic can scale with. Named rather than
#: inferred, because which population a row grows with is the part a reader
#: gets wrong: one is the operator's book and the other is how much of it is
#: broken right now.
POPULATIONS = ("configured", "unusable")

#: How far the measured rate may sit under the documented one before the
#: document is called generous. Both directions matter: too LOW a document reds
#: when a field is added (the point), and too HIGH a document would let a row
#: grow by half again with nothing saying so, or let the row quietly stop
#: carrying its per-symbol detail.
RATE_TOLERANCE = 0.70


def _diagnostic_row_sizes(
    tmp_path: Path, symbols: int, usable: int = 1
) -> dict[str, int]:
    """The two diagnostic rows' sizes for a book of `symbols`, `usable` seeded.

    THE TWO COUNTS MOVE INDEPENDENTLY, which is the whole reason this takes two
    arguments. Seeding bars for `usable` of them makes `configured` equal
    `symbols` and `unusable` equal `symbols - usable`, so a test can hold one
    population still and vary the other. With only ever one symbol seeded the
    two counts differ by a constant, their SLOPES are identical, and a row
    exempted against the wrong population would pass: measured, and it is why
    this signature is not the simpler one.
    """
    names = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD", "NZDUSD"]
    while len(names) < symbols:
        names.append(f"SYM{len(names):03d}")
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.symbols = names[:symbols]
    broker = PaperBroker(balance=10_000)
    for n, name in enumerate(cfg.symbols[:usable]):
        broker.seed_bars(
            name, generate_bars(120, drift=0.0004, vol=0.0002, seed=3 + n)
        )
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), advisor=None)
    engine.start()
    sizes = {}
    for row in engine.journal.tail(5000):
        event = row.get("event")
        if event in ROWS_EXEMPT_FROM_THE_BOUND:
            sizes[event] = max(
                sizes.get(event, 0), len(json.dumps(row, sort_keys=True))
            )
    engine.stop()
    return sizes


def _population_pairs(tmp_path: Path) -> dict[str, tuple[dict, dict, int]]:
    """For each population, two books in which ONLY that count moves.

    This is what makes the `population` field on an exemption load-bearing
    rather than decorative. A pair where both counts move by the same amount
    gives both populations the same slope, so a row declared against the wrong
    one passes: measured, by mutation, and it is why the fixtures are built per
    population instead of once.

      configured: 8 symbols / 1 seeded  ->  24 symbols / 17 seeded   (+16, +0)
      unusable:  24 symbols / 17 seeded ->  24 symbols / 1 seeded    (+0, +16)
    """
    small_book = _diagnostic_row_sizes(tmp_path / "small-book", symbols=8, usable=1)
    wide_book = _diagnostic_row_sizes(tmp_path / "wide-book", symbols=24, usable=17)
    broken_book = _diagnostic_row_sizes(tmp_path / "broken-book", symbols=24, usable=1)
    return {
        "configured": (small_book, wide_book, 16),
        "unusable": (wide_book, broken_book, 16),
    }


def test_the_documented_growth_rate_is_the_measured_one(tmp_path: Path) -> None:
    """#236. A diagnostic has a documented RATE, and it is checked both ways.

    The 512 byte bound was never true of these rows: `history_preflight`
    measured 876 bytes and `history_unavailable` 1108 on a four-symbol book,
    and both grow linearly with the book. Summarising them was considered and
    rejected on measurement, because the row repeats no boilerplate: the
    "check the symbol name" advice is written once and every other byte is a
    per-symbol fact, so a summary could only delete facts, and only during the
    incident that makes somebody read the row.

    So the contract states a rate PER POPULATION, and this is the gate. Each
    row's slope is measured on the pair of books where its OWN declared
    population moves and the other is held, which is what makes that field
    checkable: a row declared against the wrong population is measured where it
    does not move, and fails the lower bound.

    BOTH DIRECTIONS, which is the half an exemption never had:

    * measured <= documented, so a field added to the per-symbol entry reds
      here rather than quietly making every row bigger;
    * measured >= documented * RATE_TOLERANCE, so the document cannot be set
      generously to stop reding, and a row that STOPS carrying its per-symbol
      detail also reds instead of passing a ceiling it no longer approaches.
    """
    pairs = _population_pairs(tmp_path)

    for event, rate in ROWS_EXEMPT_FROM_THE_BOUND.items():
        a, b, delta = pairs[rate.population]
        assert event in a and event in b, (
            f"{event!r} was not written at both ends of the {rate.population!r} "
            f"pair, so no slope can be measured: {a!r} {b!r}"
        )
        slope = (b[event] - a[event]) / delta
        assert slope <= rate.bytes_per_item, (
            f"{event!r} grows at {slope:.0f} bytes per {rate.population} symbol, "
            f"over the documented {rate.bytes_per_item}. Either a field was "
            "added to the per-symbol entry, in which case raise the documented "
            "rate in docs/CONTRACT.md deliberately, or the row gained "
            "something that does not belong in it."
        )
        assert slope >= rate.bytes_per_item * RATE_TOLERANCE, (
            f"{event!r} grows at only {slope:.0f} bytes per {rate.population} "
            f"symbol against a documented {rate.bytes_per_item}. Either it does "
            "not scale with that population at all, or the row stopped carrying "
            "its per-symbol detail, or the documented rate is generous enough "
            "that it could never red."
        )


def test_each_row_responds_to_ITS_population_and_not_the_other(
    tmp_path: Path,
) -> None:
    """The distinction the ruling asked for, and the reason it needs a test.

    `history_preflight` grows with the operator's whole BOOK and is stable.
    `history_unavailable` grows with the UNUSABLE count, which is near zero on
    a healthy desk and spikes to the whole book during exactly the incident
    somebody is reading the row to understand. One documented rate for both
    would hide that.

    FOUND BY MUTATION: declaring the prose row against `configured` instead of
    `unusable` passed, because with one symbol seeded the two counts differ by
    a constant and their slopes are identical. A claim about WHICH population a
    row scales with is only testable if the two can move independently, so this
    holds one still and varies the other.

    * configured 8 -> 24 with unusable held at 7: preflight must grow, the
      prose row must not.
    * unusable 7 -> 23 with configured held at 24: the prose row must grow,
      preflight must not.
    """
    #: Held-still is not expected to be byte-identical: the fixed head of each
    #: row carries counts that gain digits. A response under this many bytes
    #: per item is "did not scale with it".
    flat = 20

    base = _diagnostic_row_sizes(tmp_path / "a", symbols=8, usable=1)
    wider_book = _diagnostic_row_sizes(tmp_path / "b", symbols=24, usable=17)
    more_broken = _diagnostic_row_sizes(tmp_path / "c", symbols=24, usable=1)

    pre_per_configured = (wider_book["history_preflight"] - base["history_preflight"]) / 16
    prose_per_configured = (
        wider_book["history_unavailable"] - base["history_unavailable"]
    ) / 16
    pre_per_unusable = (
        more_broken["history_preflight"] - wider_book["history_preflight"]
    ) / 16
    prose_per_unusable = (
        more_broken["history_unavailable"] - wider_book["history_unavailable"]
    ) / 16

    assert pre_per_configured > flat, (
        f"history_preflight gained only {pre_per_configured:.0f} bytes per "
        "configured symbol, so it does not scale with the book as documented"
    )
    assert abs(prose_per_configured) < flat, (
        f"history_unavailable moved {prose_per_configured:.0f} bytes per "
        "configured symbol while the unusable count was held, so it is not "
        "the unusable count it scales with"
    )
    assert prose_per_unusable > flat, (
        f"history_unavailable gained only {prose_per_unusable:.0f} bytes per "
        "unusable symbol, so it does not scale with the incident as documented"
    )
    assert abs(pre_per_unusable) < flat, (
        f"history_preflight moved {pre_per_unusable:.0f} bytes per unusable "
        "symbol while the book was held, so it is not the book it scales with"
    )


def test_an_exemption_cannot_be_claimed_without_a_rate_and_a_reason(
    tmp_path: Path,
) -> None:
    """The empty-reason hole, closed. A review measured that it passed.

    `_assert_every_row_in_bound` now checks every entry in the map, so this
    drives the check against deliberately broken entries rather than waiting
    for somebody to add one. Each of the three must be rejected: an unknown
    population, a zero rate, and a blank reason.
    """
    del tmp_path
    # Run the REAL checker, the one `_assert_every_row_in_bound` calls, against
    # deliberately broken entries. Re-implementing the predicate here would
    # test this test.
    for bad, why in (
        (GrowthRate(population="vibes", bytes_per_item=200, why="x"), "unknown population"),
        (GrowthRate(population="configured", bytes_per_item=0, why="x"), "zero rate"),
        (GrowthRate(population="configured", bytes_per_item=200, why="   "), "blank reason"),
    ):
        assert bad_exemptions({"row": bad}), (
            f"an exemption with a {why} would be accepted: {bad!r}"
        )
    # And the real map must come back clean, so the control is measuring the
    # breakage rather than a checker that always finds something.
    assert not bad_exemptions(ROWS_EXEMPT_FROM_THE_BOUND)


# --- 3. the controls: nothing that fits is reshaped ------------------------


def test_a_real_symbol_is_never_reshaped() -> None:
    """Every real instrument name, vendor suffix and all, survives whole."""
    for name in ("EURUSD", "EURUSDm", "EURUSD.a", "EURUSD_i", "XAUUSD", "BTCUSD",
                 "EURUSDmicro", "US30", "MATICUSD"):
        assert clip_for_record(name) == name, name
    assert len("EURUSDmicro") < RECORD_STRING_CHARS


def test_the_documented_bound_is_the_asserted_bound(tmp_path: Path) -> None:
    """The figure in `docs/CONTRACT.md` is read, not trusted.

    The constant and the tests already agreed, because the tests import it.
    The DOCUMENT carried its own literal and nothing read it, so a review
    measured raising `RECORD_ROW_BOUND` to 1024 with the whole suite green
    while the contract still said 512. A bound stated in prose that nothing
    reads is the magic number this issue set out to remove, one file over.

    Anchored to the `Journal row size` ROW rather than to the file, for the
    reason #220 measured: a number mentioned anywhere would satisfy a
    file-wide search.
    """
    del tmp_path
    text = (Path(__file__).resolve().parents[1] / "docs" / "CONTRACT.md").read_text(
        encoding="utf-8"
    )
    rows = [ln for ln in text.splitlines() if ln.startswith("| Journal row size |")]
    assert len(rows) == 1, f"expected one `Journal row size` row, found {len(rows)}"
    found = re.findall(r"\((\d+) bytes\)", rows[0])
    assert found, "the row no longer states a byte figure: " + rows[0][:120]
    assert [int(n) for n in found] == [RECORD_ROW_BOUND] * len(found), (
        f"docs/CONTRACT.md states {found!r} bytes and journal.RECORD_ROW_BOUND "
        f"is {RECORD_ROW_BOUND}; the documented bound and the asserted bound "
        "have drifted"
    )


def test_an_ordinary_turn_is_byte_for_byte_what_it_was(tmp_path: Path) -> None:
    """The control that stops the bound being satisfied by mangling everything."""
    engine = _engine(
        tmp_path,
        _claude_reply({
            "text": "the book is flat", "action": "buy", "symbol": "EURUSD",
            "sl": 1.09, "tp": 1.11, "limit": None, "stop": None, "ticket": None,
            "summary": "take it",
        }),
    )
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    row = _turn(engine)
    assert row["symbol"] == "EURUSD"
    assert row["sl"] == 1.09
    assert row["degraded"] == ""
    assert "[+" not in json.dumps(row)
    engine.stop()
