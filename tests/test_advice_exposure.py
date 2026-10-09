"""The advice snapshot must carry the currency exposure the gate already computes.

`llm.SYSTEM` tells the model to cover "allocation, correlation, unused risk
room". Before this suite `Engine.advice_context()` handed it raw positions and
no aggregate, so the model had to re-derive net currency exposure by eye from a
position list: the arithmetic an LLM is worst at, on the one question the
operator most wants to ask.

The failure mode pinned here is NOT the string being present. It is the two
numbers DISAGREEING -- `Engine.exposure_text()` reporting one net exposure while
`RiskManager.evaluate` refuses on another. That is the version-skew defect this
repo already recorded in #142, and it is why `exposure_text` reads
`risk.currency_exposure` instead of carrying a second implementation.

Two of these tests therefore do not read the text at all for their verdict.
`test_room_zero_is_the_gate_refusing` and its positive control assert that the
reported `room` predicts what `evaluate` actually does, so the report and the
refusal cannot drift apart without one of them going red.
"""

import re
from datetime import datetime, timezone

from straightedge.broker.paper import PaperBroker
from straightedge.config import AdviceConfig, BotConfig
from straightedge.engine import Engine
from straightedge.llm import Advisor
from straightedge.models import PendingOrder, Position
from straightedge.risk import currency_exposure
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand

# `room` accepts a MINUS on purpose (#175). The clamp is what keeps room
# non-negative, so a parser that cannot match a negative cannot observe the
# clamp failing: the row would simply stop matching and the test would red on
# a KeyError instead of on the number it exists to check.
_ROW = re.compile(r"^([A-Z]{2,8}) net=([+-]\d+) room=(-?\d+)$")


class FakeLlm:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.sent: list[tuple[str, dict]] = []

    def post_json(self, url: str, payload: dict, timeout: float = 10.0, headers=None) -> dict:
        self.sent.append((url, payload))
        return self.payload


def _engine(tmp_path, *, symbols=("EURUSD", "GBPUSD", "AUDUSD"), llm=None) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.symbols = list(symbols)
    broker = PaperBroker(balance=10_000)
    seed = 3
    for name in ("EURUSD", "GBPUSD", "AUDUSD", "EURJPY", "EURGBP", "DOGEUSD", "US30"):
        broker.seed_bars(name, generate_bars(120, drift=0.0004, vol=0.0002, seed=seed))
        seed += 1
    advisor = None
    if llm is not None:
        advisor = Advisor(AdviceConfig(provider="grok", grok_key="x"), transport=llm)
    return Engine(
        cfg,
        broker,
        halt_dir=str(tmp_path),
        advisor=advisor,
        now_fn=lambda: datetime(2024, 1, 3, 12, tzinfo=timezone.utc),
    )


def _open(engine: Engine, cmd: str, seq: int) -> str:
    """Stage and confirm one market order, returning the desk's own reply."""
    staged = engine.handle_command(TgCommand("1", 1, cmd, seq))
    if staged.startswith("refused"):
        return staged
    return engine.handle_command(TgCommand("1", 1, "/confirm", seq + 1))


def _parse(blob: str) -> tuple[int | None, dict[str, int], dict[str, int]]:
    """cap, net-per-code and room-per-code, read back out of the rendered text."""
    cap: int | None = None
    net: dict[str, int] = {}
    room: dict[str, int] = {}
    for line in blob.splitlines():
        if line.startswith("currency_exposure cap="):
            cap = int(line.split("cap=")[1].split()[0])
            continue
        match = _ROW.match(line)
        if match:
            net[match.group(1)] = int(match.group(2))
            room[match.group(1)] = int(match.group(3))
    return cap, net, room


def _committed(engine: Engine) -> list[Position | PendingOrder]:
    magic = engine.cfg.risk.magic
    return [*engine.broker.positions(magic=magic), *engine.broker.orders(magic=magic)]


def _plant_two_usd_shorts(engine: Engine) -> None:
    """Buy EURUSD and GBPUSD: USD reaches -2, which is exactly the cap."""
    assert _open(engine, "/buy EURUSD", 1).startswith("sent buy"), "EURUSD leg did not open"
    assert _open(engine, "/buy GBPUSD", 3).startswith("sent buy"), "GBPUSD leg did not open"


def test_exposure_matches_the_gates_own_computation(tmp_path) -> None:
    """The reported net per currency IS risk.currency_exposure over the same book.

    Compared against the function the gate calls, not against a literal, so a
    second implementation that drifts from the gate reds here rather than
    agreeing with a hand-copied expectation that drifted with it.
    """
    engine = _engine(tmp_path)
    engine.start()
    _plant_two_usd_shorts(engine)

    expected = currency_exposure(_committed(engine))
    cap, net, room = _parse(engine.exposure_text())

    assert cap == engine.cfg.risk.max_currency_exposure
    assert net == expected, f"reported {net}, gate computes {expected}"
    assert room == {code: cap - abs(v) for code, v in expected.items()}
    engine.stop()


def test_room_zero_is_the_gate_refusing(tmp_path) -> None:
    """room=0 must mean evaluate() actually refuses the next leg on that currency.

    This is the un-fix test: the verdict comes from the DESK, not from the
    string. If exposure_text ever reports room the gate does not honour, the
    two assertions below cannot both hold.
    """
    engine = _engine(tmp_path)
    engine.start()
    _plant_two_usd_shorts(engine)

    _cap, net, room = _parse(engine.exposure_text())
    assert net["USD"] == -2
    assert room["USD"] == 0

    # A third USD-short leg. Two commitments only, so max_positions (3) cannot
    # be what refuses this; the reason has to be the exposure gate itself.
    assert len(_committed(engine)) == 2
    assert engine.handle_command(TgCommand("1", 1, "/buy AUDUSD", 5)) == (
        "refused: currency_exposure"
    )
    engine.stop()


def test_room_above_zero_is_not_refused_on_exposure(tmp_path) -> None:
    """Positive control: a currency the report says has room is genuinely open.

    Without this, a report that printed room=0 for everything would pass the
    test above while being useless. EUR is at +1 of a cap of 2, and EURJPY
    leaves USD untouched at -2, which is at the cap but not over it.
    """
    engine = _engine(tmp_path)
    engine.start()
    _plant_two_usd_shorts(engine)

    _cap, net, room = _parse(engine.exposure_text())
    assert net["EUR"] == 1
    assert room["EUR"] == 1

    reply = engine.handle_command(TgCommand("1", 1, "/buy EURJPY", 5))
    assert "currency_exposure" not in reply, reply
    engine.stop()


def test_empty_book_still_states_the_cap(tmp_path) -> None:
    """A model cannot reason about room from an absent number.

    The cap is configuration, not a fact about the book, so it is reported
    whether or not anything is committed.
    """
    engine = _engine(tmp_path)
    engine.start()
    text = engine.exposure_text()
    cap, net, _room = _parse(text)
    assert cap == engine.cfg.risk.max_currency_exposure
    assert net == {}
    assert "no currency commitments" in text
    engine.stop()


def test_non_fx_commitment_is_named_not_silently_dropped(tmp_path) -> None:
    """The issue #10 decision, carried into the report: not-applicable is never silence.

    US30 contributes nothing to currency exposure, which is correct rather than
    an underestimate. The model must be told the aggregate excludes it, or the
    one number it is given quietly covers less of the book than it appears to.
    """
    engine = _engine(tmp_path, symbols=("EURUSD", "US30"))
    engine.start()
    assert _open(engine, "/buy US30", 1).startswith("sent buy")
    text = engine.exposure_text()
    assert "excluded_from_currency_limit=US30" in text
    engine.stop()


def test_unreadable_orders_declare_the_exposure_incomplete(tmp_path) -> None:
    """An unreadable order book must not render as a confident total.

    risk_text already refuses to print a committed count it cannot stand
    behind (`held+?`). Exposure has the same obligation: a resting order is
    committed exposure, so a number computed without the working orders is an
    UNDERSTATEMENT of the book, and understating exposure to a model asked
    about unused risk room is the dangerous direction to be wrong in.
    """
    engine = _engine(tmp_path)
    engine.start()
    _plant_two_usd_shorts(engine)

    def boom(magic=None):
        raise RuntimeError("orders unreadable")

    engine.broker.orders = boom
    text = engine.exposure_text()
    assert "INCOMPLETE" in text, text
    engine.stop()


def test_exposure_reaches_the_advice_context(tmp_path) -> None:
    """End of the chain: what the model is actually sent.

    exposure_text can be correct and still never reach the snapshot, so this
    asserts on the payload the transport received rather than on the helper.
    """
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Hold.\n"
                        '{"action":"hold","symbol":null,"sl":null,"tp":null,"summary":"x"}'
                    )
                }
            }
        ]
    }
    llm = FakeLlm(payload)
    engine = _engine(tmp_path, llm=llm)
    engine.start()
    _plant_two_usd_shorts(engine)
    engine.handle_command(TgCommand("1", 1, "/ask where is my risk?", 5))

    blob = str(llm.sent[0][1])
    assert "currency_exposure cap=" in blob
    assert "USD net=-2 room=0" in blob
    engine.stop()


def test_risk_text_states_the_room_the_prompt_promises(tmp_path) -> None:
    """SYSTEM names "daily_loss room" and "drawdown room" as things the model has.

    The line carried loss/cap and drawdown/cap, so room was derivable but not
    stated, leaving the same subtraction to the model that the exposure block
    exists to stop. Reporting only; no gate reads this text.
    """
    engine = _engine(tmp_path)
    engine.start()
    text = engine.risk_text()
    assert re.search(r"daily_loss=[\d.]+/[\d.]+ room=[\d.]+", text), text
    assert re.search(r"drawdown=[\d.]+/[\d.]+ room=[\d.]+", text), text
    engine.stop()


def test_a_resting_order_counts_in_the_reported_exposure(tmp_path) -> None:
    """A working order is committed exposure, so the aggregate must count it.

    Its own test because every other book planted in this file is positions
    only: without it, an `exposure_text` that read `broker.positions()` and
    forgot `broker.orders()` would pass the whole suite while understating the
    book by exactly the legs that become positions with nobody being asked
    again. `evaluate` counts them (`committed = [*ours, *ours_orders]`), so the
    report has to.
    """
    engine = _engine(tmp_path)
    engine.start()
    assert _open(engine, "/buy EURUSD", 1).startswith("sent buy")

    # Priced well below the market and given its own sl/tp, so the leg RESTS
    # rather than filling and the rr gate has something to measure. EURGBP
    # because it touches neither USD nor the JPY spec's wide synthetic spread.
    limit = round(engine.broker.tick("EURGBP").bid * 0.99, 5)
    cmd = f"/buy EURGBP limit={limit} sl={round(limit * 0.995, 5)} tp={round(limit * 1.02, 5)}"
    staged = engine.handle_command(TgCommand("1", 1, cmd, 3))
    assert not staged.startswith("refused"), staged
    sent = engine.handle_command(TgCommand("1", 1, "/confirm", 4))
    assert sent.startswith("sent"), sent

    assert len(engine.broker.orders(magic=engine.cfg.risk.magic)) == 1, (
        "the EURJPY leg must still be RESTING; a filled one proves nothing here"
    )

    _cap, net, _room = _parse(engine.exposure_text())
    assert net == currency_exposure(_committed(engine))
    assert net["EUR"] == 2, "the resting order's EUR leg is committed exposure"
    assert net["GBP"] == -1
    engine.stop()


def test_a_non_three_letter_code_is_netted_the_way_the_gate_nets_it(tmp_path) -> None:
    """The discriminating case for "do not write a second aggregation".

    Every other book in this file is built from 3+3 symbols, where a naive
    `symbol[:3]` / `symbol[3:6]` split agrees with `risk.currency_exposure` by
    accident. It was measured doing exactly that: an un-fix that replaced the
    shared function with a 3-and-3 split passed this whole suite until this
    test existed, which made the suite decoration on the one defect it was
    written to catch.

    DOGEUSD resolves through the code table to DOGE/USD (#98, commit ca20809
    made the mt4 adapter stop guessing the same way). A split reads DOG/EUS,
    so the two implementations cannot both be right here.
    """
    engine = _engine(tmp_path, symbols=("DOGEUSD",))
    engine.start()
    assert _open(engine, "/buy DOGEUSD", 1).startswith("sent buy")

    expected = currency_exposure(_committed(engine))
    assert expected == {"DOGE": 1, "USD": -1}, expected

    _cap, net, _room = _parse(engine.exposure_text())
    assert net == expected, f"reported {net}, gate computes {expected}"
    engine.stop()


# The three tests below exist because of a FIXTURE DISCRIMINATION AUDIT, run
# after the DOGEUSD lesson generalised: for each plausible wrong
# implementation, can these fixtures tell it apart from the right one? Four
# mutations passed the whole suite, and each one is pinned here. They were all
# the same mistake, which is worth naming once: every assertion was a PRESENCE
# or a SHAPE check, and every fixture used the same default config value, so a
# constant agreed with all of them.


def test_a_non_default_cap_is_read_from_config_not_assumed(tmp_path) -> None:
    """Measured: `cap = 2` hardcoded in exposure_text passed all 75 tests.

    Every other fixture here runs at `max_currency_exposure = 2`, the default,
    so no assertion could tell a config read from a literal. USD is the
    discriminating code: at cap 3 with net -2 the room is 1, where a hardcoded
    cap gives 0, `abs(net)` gives 2 and `cap - net` gives 5. All four answers
    differ, which is the property the fixture has to have.
    """
    engine = _engine(tmp_path)
    engine.cfg.risk.max_currency_exposure = 3
    engine.start()
    _plant_two_usd_shorts(engine)

    cap, net, room = _parse(engine.exposure_text())
    assert cap == 3, "the cap is configuration, not a constant"
    assert net["USD"] == -2
    assert room["USD"] == 1
    assert room["EUR"] == 2
    engine.stop()


def test_a_readable_all_fx_book_declares_no_warning_lines(tmp_path) -> None:
    """Negative control for both conditional lines.

    Measured: printing `INCOMPLETE` unconditionally passed all 75 tests, and so
    did printing `excluded_from_currency_limit=US30` unconditionally, because
    every assertion on them was a presence check and nothing asserted their
    ABSENCE. A warning that is always on carries no information: it would tell
    the model the aggregate is unreliable, and that part of the book is
    uncovered, on every healthy book it ever sees.
    """
    engine = _engine(tmp_path)
    engine.start()
    _plant_two_usd_shorts(engine)

    text = engine.exposure_text()
    assert "INCOMPLETE" not in text, text
    assert "excluded_from_currency_limit" not in text, text
    engine.stop()


def test_risk_text_room_is_the_remaining_budget_not_a_constant(tmp_path) -> None:
    """Measured: `room=0.00` hardcoded in risk_text passed all 75 tests.

    `test_risk_text_states_the_room_the_prompt_promises` asserts the SHAPE with
    a regex, which a constant satisfies. This asserts the VALUE against the
    engine's own snapshot arithmetic, on a book that has moved equity off
    `day_start` so that zero is the wrong answer.
    """
    engine = _engine(tmp_path)
    engine.start()
    _plant_two_usd_shorts(engine)

    text = engine.risk_text()
    # risk_text calls observe() itself, so read the snapshot it left behind
    # rather than a second one computed from a different account read.
    acct = engine.broker.account()
    snap = engine.risk.snapshot
    r = engine.cfg.risk
    daily_loss = snap.day_start_equity - acct.equity
    daily_cap = snap.day_start_equity * r.daily_loss_pct
    dd = snap.peak_equity - acct.equity
    dd_cap = snap.peak_equity * r.max_drawdown_pct

    assert daily_loss > 0, "the fixture must move equity, or zero would be right"
    assert (
        f"daily_loss={daily_loss:.2f}/{daily_cap:.2f} "
        f"room={max(daily_cap - daily_loss, 0.0):.2f}"
    ) in text, text
    assert (
        f"drawdown={dd:.2f}/{dd_cap:.2f} room={max(dd_cap - dd, 0.0):.2f}"
    ) in text, text
    assert "room=0.00" not in text, "a constant zero satisfies a shape-only assertion"
    engine.stop()


def test_a_cap_lowered_below_the_open_book_reports_zero_room(tmp_path) -> None:
    """The `max(..., 0)` clamp, which no instrument could see before this (#175).

    Strummer swept all 55 `(cap, net)` pairs for cap 0..4 and net -5..+5 against
    #169: THIRTY report negative room unclamped, and every one of them needs
    `abs(net) > cap`. That state cannot arise from the gate's own accounting,
    because the gate refuses the commitment that would cross the cap. It needs
    either a cap LOWERED in config after positions were already open, or foreign
    positions carrying our magic.

    This takes the lowered-cap route: reachable through config alone, no
    foreign-magic fixture, and the same shape as the non-default-cap fixture.

    Why it is worth a test even though it is cosmetic: the number is reported
    INTO THE ADVICE PROMPT. A negative budget is not merely wrong, it invites
    the model to treat room as a signed quantity it can spend back toward zero.
    """
    engine = _engine(tmp_path)
    engine.start()
    _plant_two_usd_shorts(engine)

    # The operator tightens the limit while the book is already open. Nothing
    # here asks the engine to permit anything, so this changes no decision.
    engine.cfg.risk.max_currency_exposure = 1

    text = engine.exposure_text()
    cap, net, room = _parse(text)
    assert cap == 1
    assert net["USD"] == -2, "the book must exceed the new cap, or there is nothing to clamp"
    assert abs(net["USD"]) > cap, (net["USD"], cap)

    # Unclamped this is cap - abs(net) = 1 - 2 = -1.
    assert room["USD"] == 0, f"reported room {room['USD']} for USD; the clamp did not hold"
    assert all(v >= 0 for v in room.values()), room
    assert "room=-" not in text, text
    engine.stop()


def test_a_zero_cap_over_an_open_book_reports_zero_room_everywhere(tmp_path) -> None:
    """The strongest point in the sweep: every leg goes negative unclamped.

    At cap 0 all three codes are over, so a single-currency assertion cannot be
    what passes here. Separate from the test above because that one leaves EUR
    and GBP at exactly zero room rather than past it, so it does not exercise
    the clamp on them.
    """
    engine = _engine(tmp_path)
    engine.start()
    _plant_two_usd_shorts(engine)
    engine.cfg.risk.max_currency_exposure = 0

    text = engine.exposure_text()
    cap, net, room = _parse(text)
    assert cap == 0
    # The codes come from the ENGINE's own computation, not from a literal.
    # Hardcoding {"EUR","GBP","USD"} coupled this test to which symbols
    # `_plant_two_usd_shorts` happens to buy: #176 changes its second leg to
    # LINKUSD so the book is not 3+3, and the two changes merge CLEANLY while
    # the combined suite reds. Measured, not predicted. A clean merge is not a
    # passing merge, and an assertion naming a fixture detail it does not care
    # about is what turns an unrelated fixture edit into a failure.
    assert set(room) == set(currency_exposure(_committed(engine)))
    assert all(abs(net[c]) > cap for c in room), net
    assert set(room.values()) == {0}, room
    assert "room=-" not in text, text
    engine.stop()


def test_no_negative_room_reaches_the_advice_prompt(tmp_path) -> None:
    """End of the chain: the snapshot the model is actually handed.

    `exposure_text` can be clamped and the context still carry something else,
    and the prompt is where the number does its damage.
    """
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Hold.\n"
                        '{"action":"hold","symbol":null,"sl":null,"tp":null,"summary":"x"}'
                    )
                }
            }
        ]
    }
    llm = FakeLlm(payload)
    engine = _engine(tmp_path, llm=llm)
    engine.start()
    _plant_two_usd_shorts(engine)
    engine.cfg.risk.max_currency_exposure = 1
    engine.handle_command(TgCommand("1", 1, "/ask how much room is left?", 5))

    blob = str(llm.sent[0][1])
    assert "USD net=-2 room=0" in blob
    assert "room=-" not in blob, blob
    engine.stop()
