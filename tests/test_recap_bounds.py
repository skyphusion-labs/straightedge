"""straightedge#119: the nightly recap must not compound, repeat, or burst.

Three defects, one function. These tests are written to fail on the code that
shipped them, and each one asserts a PROPERTY rather than a rendering:

- The recap stored `tail=self.history_text(8)`, a rendering of OTHER journal
  rows, as a field inside its own journal row. `history_text` renders every
  field of every row it reads, so it re-expanded that stored `tail` on the next
  recap: recap N contained recap N-1 contained recap N-2. Asserting that a
  recap "mentions the right day" passes happily on a payload nested fifty deep,
  so the assertions here are on SIZE and STRUCTURE.
- `_maybe_daily_recap` never advanced `snap.day_key`. The rollover lives in
  `RiskManager.observe`, reached through `_apply_circuit`, which `step_all`
  returns BEFORE when `self.halted`. So a halted desk re-emitted the recap on
  every tick, forever.
- One logical message was chunked into as many Telegram sends as it took.

The halted test drives the halt through the operator HALT file rather than
setting `engine.halted` by hand: the flag is what `step_all` branches on, and
reaching it the way production does is what makes the test evidence about the
shipped path instead of about my own assumption.
"""

import json
from datetime import datetime, timedelta, timezone

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import Engine
from straightedge.synthetic import generate_bars
from straightedge.telegram import TelegramClient, TgCommand

RECAP_NOTIFY = frozenset({"start", "stop", "open", "close", "halt", "recap"})


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.updates: list[dict] = []

    def post_json(self, url: str, payload: dict, timeout: float = 10.0, headers=None) -> dict:
        del timeout, headers
        self.sent.append((url, payload))
        if url.endswith("/getUpdates"):
            result = self.updates
            self.updates = []
            return {"ok": True, "result": result}
        return {"ok": True, "result": {"message_id": 1}}


def _cfg(tmp_path) -> BotConfig:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.strategy.auto = False
    return cfg


def _engine(tmp_path, clock, *, telegram=None, cfg=None) -> Engine:
    cfg = cfg if cfg is not None else _cfg(tmp_path)
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    return Engine(
        cfg,
        broker,
        halt_dir=str(tmp_path),
        telegram=telegram,
        now_fn=lambda: clock[0],
    )


def _recaps(engine: Engine) -> list[dict]:
    return [r for r in engine.journal.tail(10_000) if r.get("event") == "recap"]


def _roll_days(engine: Engine, clock, days: int) -> None:
    """Advance whole UTC days, one `step_all` each, the way a live desk ticks."""
    for _ in range(days):
        clock[0] = (clock[0] + timedelta(days=1)).replace(hour=0, minute=5)
        engine.step_all()


def test_recap_payload_does_not_grow_with_the_number_of_days(tmp_path) -> None:
    """The property: N days of recaps, and recap N is not bigger than recap 1.

    This is the assertion the stored `tail` cannot survive. Each recap embedded
    the previous one, so the payload grew multiplicatively with N while every
    "does it name the right day" check stayed green.
    """
    clock = [datetime(2024, 1, 3, 12, tzinfo=timezone.utc)]
    engine = _engine(tmp_path, clock)
    engine.start()
    _roll_days(engine, clock, 8)
    engine.stop()

    rows = _recaps(engine)
    assert len(rows) == 8, f"expected one recap per day roll, got {len(rows)}"
    sizes = [len(json.dumps(r, sort_keys=True)) for r in rows]
    # Not "small": FLAT. A recap's own facts (day, equity, day_start, pnl) are
    # the same shape every day, so the last row may differ from the first only
    # by the digits in its numbers.
    assert max(sizes) - min(sizes) <= 32, f"recap payload grows with N: {sizes}"
    assert max(sizes) <= 512, f"a recap row carries more than its own facts: {sizes}"


def test_a_recap_row_never_stores_a_rendering_of_other_rows(tmp_path) -> None:
    """The structural half: a journal row holds FACTS, never rendered rows.

    Size alone could be satisfied by a cap on a field that still holds a
    rendering. What makes the design wrong is that the field exists at all, so
    assert on the shape: no value may be multi-line, and no value may contain
    another row's event name or timestamp.
    """
    clock = [datetime(2024, 1, 3, 12, tzinfo=timezone.utc)]
    engine = _engine(tmp_path, clock)
    engine.start()
    _roll_days(engine, clock, 4)
    engine.stop()

    rows = _recaps(engine)
    assert rows, "no recap was emitted, so this test measured nothing"
    for row in rows:
        for key, value in row.items():
            if key in {"ts", "event"} or not isinstance(value, str):
                continue
            assert "\n" not in value, f"recap field {key} holds a rendered block"
            assert "recap" not in value, f"recap field {key} embeds another recap"
            assert "T00:0" not in value, f"recap field {key} embeds another row's ts"


def test_a_halted_desk_emits_exactly_one_recap_per_day(tmp_path) -> None:
    """Defect 2, and the dangerous one: the recap re-fired on every halted tick.

    `step_all` calls `_maybe_daily_recap` and THEN returns early on
    `self.halted`, before the `_apply_circuit` path that rolls `day_key`. So
    while halted the day never rolled and the emit condition stayed true. Ten
    ticks across one midnight is ten recaps; it must be one.
    """
    tr = FakeTransport()
    tg = TelegramClient(token="t", chat_id="1", transport=tr, notify_events=RECAP_NOTIFY)
    clock = [datetime(2024, 1, 3, 12, tzinfo=timezone.utc)]
    engine = _engine(tmp_path, clock, telegram=tg)
    engine.start()

    # Halt the way an operator does. This is what sets engine.halted, and the
    # early return it causes is the whole defect.
    engine.risk.write_halt_file("operator")
    engine.step_all()
    assert engine.halted, "the HALT file did not halt the desk, so nothing was tested"
    assert not _recaps(engine), "a recap fired before any day rolled"

    clock[0] = datetime(2024, 1, 4, 0, 5, tzinfo=timezone.utc)
    for _ in range(10):
        engine.step_all()
    assert engine.halted, "the desk un-halted mid-test"

    rows = _recaps(engine)
    assert len(rows) == 1, f"halted desk emitted {len(rows)} recaps across one midnight"
    assert rows[0].get("day") == "2024-01-03"
    texts = [p.get("text", "") for _, p in tr.sent]
    assert len([t for t in texts if t.startswith("RECAP")]) == 1
    engine.stop()


def test_a_restart_while_halted_does_not_re_send_the_recap(tmp_path) -> None:
    """A crash loop must not re-send the recap, and this one PASSED before the fix.

    Said plainly so nobody reads it as evidence for the marker: the emitted
    marker is in-process memory, so a restart clears it, and this test still
    passes either way. It passes because `start()` calls `risk.observe()` before
    the first tick, which rolls `snap.day_key` to today; `_maybe_daily_recap`
    then cannot owe a recap for a day that ended before the restart.

    It is kept because that reasoning is load-bearing and invisible at the call
    site. Remove the `observe()` from `start()` and this test goes red, which is
    the reachable world in which it fails. It is a pin on the argument for NOT
    persisting the marker, not a proof of defect 2.

    The consequence this used to pin as out of scope, a desk restarted across
    midnight never sending the recap for the day that ended, is FIXED in
    straightedge#129. This test is unaffected and that is not luck: the first
    process here already emitted the recap for the 3rd before it stopped, so
    the restarted process reads that row and stays quiet. What the fix adds is
    an announcement for a day NOBODY recapped, which is a different state from
    this one; see the straightedge#129 cases at the end of this file.

    The reasoning above still holds for the in-process marker, with one
    correction: a recap CAN now be owed across a restart, so "a journal marker
    would guard an unreachable state" is no longer why `_recapped_day` is in
    memory. It is in memory because it guards a boundary this process watched;
    the restart case has its own marker, and that one is the journal.
    """
    clock = [datetime(2024, 1, 3, 12, tzinfo=timezone.utc)]
    cfg = _cfg(tmp_path)
    first = _engine(tmp_path, clock, cfg=cfg)
    first.start()
    first.risk.write_halt_file("operator")
    first.step_all()
    clock[0] = datetime(2024, 1, 4, 0, 5, tzinfo=timezone.utc)
    first.step_all()
    assert len(_recaps(first)) == 1, "the first process did not emit its one recap"
    first.stop()

    # Same journal, same state sidecar, same still-halted day: a fresh process.
    second = _engine(tmp_path, clock, cfg=_cfg(tmp_path))
    second.start()
    for _ in range(5):
        second.step_all()
    rows = [r for r in _recaps(second) if r.get("day") == "2024-01-03"]
    assert len(rows) == 1, f"restart re-sent the recap for a day already recapped: {len(rows)}"
    second.stop()


def test_history_text_bounds_every_row_it_renders(tmp_path) -> None:
    """The read-side guard, and it is why the live journal needs no surgery.

    Conrad's VPS journal already holds compounded rows. Dropping the field at
    the source stops NEW ones, but `/history` and `/recap` still render the old
    ones, so the burst would come back on a pull instead of a push. The bound is
    structural (one row's rendering is capped) rather than a denylist on the
    field name, so it also covers the next oversized field rather than this one.
    """
    from straightedge.engine import HISTORY_ROW_CHARS

    clock = [datetime(2024, 1, 3, 12, tzinfo=timezone.utc)]
    engine = _engine(tmp_path, clock)
    engine.start()
    # Exactly the shape the live journal is full of.
    engine.journal.write("recap", day="2024-01-03", tail="X" * 50_000)
    text = engine.history_text(5)
    engine.stop()

    assert "XXXX" in text, "the row was dropped rather than clipped"
    for line in text.splitlines():
        assert len(line) <= HISTORY_ROW_CHARS + 120, f"row rendered {len(line)} chars"
    assert "truncated" in text, "a clipped row must say that it was clipped"
    assert len(text) < 50_000, "the oversized field reached the rendering intact"


def test_the_already_polluted_journal_cannot_still_burst(tmp_path) -> None:
    """The deploy condition, and the reason no journal surgery is needed.

    Conrad's VPS journal is already full of compounded rows and a code fix does
    not shrink what is already written. Two claims in the PR depend on that
    being survivable, so neither is left as an assertion:

    - the nightly PUSH is short regardless, because `_maybe_daily_recap` no
      longer reads the journal at all; and
    - a PULL (`/recap`, `/history`) over those same rows stays inside the send
      bound, because `history_text` clips per row.

    The fixture is the real shape: eight recap rows of 50 KB each, which is what
    a fortnight of compounding produced.
    """
    from straightedge.telegram import MAX_SEND_CHUNKS

    tr = FakeTransport()
    tg = TelegramClient(token="t", chat_id="1", transport=tr, notify_events=RECAP_NOTIFY)
    clock = [datetime(2024, 1, 3, 12, tzinfo=timezone.utc)]
    engine = _engine(tmp_path, clock, telegram=tg)
    engine.start()
    for day in range(1, 9):
        engine.journal.write(
            "recap", day=f"2024-01-{day:02d}", tail="OLD-COMPOUNDED-ROW " * 2800
        )

    # The PUSH: roll a day and let the recap fire over the polluted journal.
    before = len(tr.sent)
    clock[0] = datetime(2024, 1, 4, 0, 5, tzinfo=timezone.utc)
    engine.step_all()
    pushed = [p for u, p in tr.sent[before:] if u.endswith("/sendMessage")]
    assert len(pushed) == 1, f"the nightly recap took {len(pushed)} sends"
    assert pushed[0]["text"].startswith("RECAP")
    assert "OLD-COMPOUNDED-ROW" not in pushed[0]["text"]

    # The PULL: the operator asks for the same rows by hand.
    before = len(tr.sent)
    assert tg.send(engine.recap_text()) is True
    pulled = [p for u, p in tr.sent[before:] if u.endswith("/sendMessage")]
    assert len(pulled) <= MAX_SEND_CHUNKS, f"/recap took {len(pulled)} sends"
    engine.stop()


def test_the_row_bound_still_clears_the_widest_legitimate_row(tmp_path) -> None:
    """HISTORY_ROW_CHARS is a MEASURED number, so measure it rather than trust it.

    The bound is only honest while it sits above every row an ordinary session
    writes; below that, a clip marker would stop meaning "this row is anomalous"
    and start meaning "this desk runs a lot of symbols". The widest such row is
    `history_preflight`, whose width scales with the configured book, and it was
    797 characters at four symbols when the bound was chosen.

    A comment carrying that number would drift the moment a field is added to
    the preflight row or the default book grows. This asserts it instead, so the
    change that invalidates the bound reds here and says so.
    """
    from straightedge.engine import HISTORY_ROW_CHARS

    cfg = _cfg(tmp_path)
    cfg.symbols = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
    broker = PaperBroker(balance=10_000)
    for i, name in enumerate(cfg.symbols):
        broker.seed_bars(name, generate_bars(120, drift=0.0004, vol=0.0002, seed=3 + i))
    clock = [datetime(2024, 1, 3, 12, tzinfo=timezone.utc)]
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: clock[0])
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    engine.step_all()
    engine.stop()

    widest = 0
    culprit = ""
    for rec in engine.journal.tail(500):
        extra = " ".join(
            f"{k}={v}"
            for k, v in rec.items()
            if k not in {"ts", "event"} and v not in (None, "")
        )
        if len(extra) > widest:
            widest, culprit = len(extra), str(rec.get("event", ""))

    assert widest > 0, "no rows were written, so this test measured nothing"
    assert widest <= HISTORY_ROW_CHARS, (
        f"the widest ordinary row is now {widest} chars ({culprit}), at or past the "
        f"{HISTORY_ROW_CHARS}-char bound, so legitimate rows would be clipped: "
        "re-measure the bound rather than raising it reflexively"
    )
    # And the headroom is real, not a rounding accident.
    assert widest < HISTORY_ROW_CHARS, "no headroom left above the widest real row"


def test_send_cannot_turn_one_message_into_a_burst() -> None:
    """Defect 3: the chunk loop had no ceiling, so N chunks was N notifications."""
    from straightedge.telegram import MAX_SEND_CHUNKS

    tr = FakeTransport()
    audited: list[tuple[str, dict]] = []
    tg = TelegramClient(
        token="t",
        chat_id="1",
        transport=tr,
        audit_fn=lambda event, fields: audited.append((event, fields)),
    )
    assert tg.send("Y" * 200_000) is True

    sends = [p for url, p in tr.sent if url.endswith("/sendMessage")]
    assert len(sends) <= MAX_SEND_CHUNKS, f"one message became {len(sends)} sends"
    # Telegram's own hard limit is 4096 characters per message.
    for payload in sends:
        assert len(payload["text"]) <= 4096

    # A silent truncation is a worse bug than a long message: it must be
    # visible to the operator in the chat AND recorded in the journal.
    assert "truncated" in sends[-1]["text"]
    assert [e for e, _ in audited] == ["notify_truncated"]
    assert audited[0][1]["dropped_chars"] > 0


def test_send_leaves_a_message_that_fits_completely_alone() -> None:
    """The bound must not be able to fire on a message that was always fine.

    A ceiling that clips normal traffic would be a new defect, so pin the
    negative: the longest fixed string the desk sends is HELP, and nothing
    about it may change.
    """
    from straightedge.telegram import HELP

    tr = FakeTransport()
    tg = TelegramClient(token="t", chat_id="1", transport=tr)
    assert tg.send(HELP) is True
    sends = [p for url, p in tr.sent if url.endswith("/sendMessage")]
    assert len(sends) == 1
    assert sends[0]["text"] == HELP
    assert "truncated" not in sends[0]["text"]


# --- straightedge#129: the day that ended while the desk was down ----------
#
# The counterpart to the three defects above: #119 was too many recaps, this is
# a missing one. `start()` calls `risk.observe()` before the first tick, which
# rolls `snap.day_key` to today, so `_maybe_daily_recap` can no longer owe a
# recap for the day that just ended and nothing is emitted at all. Silent: no
# journal row, no message, and nothing to tell "recapped" from "swallowed".
#
# What is emitted instead is deliberately NOT a P&L. The ended day's closing
# equity was never observed, `day_start_equity` is the only half that survives
# in the snapshot, and the issue says in as many words that a recap reporting
# the wrong baseline is worse than no recap. So the row names what it could not
# measure, in the `unmeasured` shape this repo already uses for a spec and for
# the venue clock, and carries no `pnl` field at all: a missing field cannot be
# misread, a zero can.


def _recap_days(engine: Engine) -> list[str]:
    return [str(r.get("day", "")) for r in _recaps(engine)]


def test_a_restart_across_midnight_announces_the_day_it_could_not_recap(
    tmp_path,
) -> None:
    """The defect. One process ends on the 3rd, the next starts on the 4th."""
    clock = [datetime(2024, 1, 3, 12, tzinfo=timezone.utc)]
    first = _engine(tmp_path, clock, cfg=_cfg(tmp_path))
    first.start()
    first.step_all()
    assert _recaps(first) == [], "nothing is owed yet; the day has not ended"
    first.stop()

    # The box was down across the boundary: a reboot, a deploy, a crash loop.
    clock[0] = datetime(2024, 1, 4, 8, 0, tzinfo=timezone.utc)
    second = _engine(tmp_path, clock, cfg=_cfg(tmp_path))
    second.start()
    rows = _recaps(second)
    assert len(rows) == 1, (
        "a desk restarted across midnight said nothing about the day that "
        f"ended while it was down: {rows}"
    )
    rec = rows[0]
    assert rec["day"] == "2024-01-03"
    assert rec["day_start"] == 10_000.0
    assert rec["days_skipped"] == 1
    assert sorted(rec["unmeasured"]) == ["equity", "pnl"]
    assert "pnl" not in rec, "a pnl nobody measured must not be in the row at all"
    assert "equity" not in rec
    second.stop()


def test_the_announcement_is_made_once_and_survives_a_crash_loop(tmp_path) -> None:
    """Three restarts on the same day are one announcement, not three.

    This is #119's failure mode arriving by the other door, so the marker for
    this one cannot be the in-process memory `_recapped_day` is: every restart
    clears that. The journal is what the next process reads.
    """
    clock = [datetime(2024, 1, 3, 12, tzinfo=timezone.utc)]
    first = _engine(tmp_path, clock, cfg=_cfg(tmp_path))
    first.start()
    first.step_all()
    first.stop()

    clock[0] = datetime(2024, 1, 4, 8, 0, tzinfo=timezone.utc)
    for _ in range(3):
        engine = _engine(tmp_path, clock, cfg=_cfg(tmp_path))
        engine.start()
        engine.step_all()
        engine.stop()
    seen = _recap_days(engine)
    assert seen.count("2024-01-03") == 1, f"a crash loop re-announced the day: {seen}"


def test_a_week_of_downtime_is_one_announcement_naming_the_count(tmp_path) -> None:
    """The bound belongs in the design, not in a cap bolted on after.

    A box down for a week must not emit seven recaps on boot. It emits one row
    for the last day it actually observed, and states how many boundaries it
    missed, which is bounded by construction.
    """
    clock = [datetime(2024, 1, 3, 12, tzinfo=timezone.utc)]
    first = _engine(tmp_path, clock, cfg=_cfg(tmp_path))
    first.start()
    first.step_all()
    first.stop()

    clock[0] = datetime(2024, 1, 10, 9, 0, tzinfo=timezone.utc)
    second = _engine(tmp_path, clock, cfg=_cfg(tmp_path))
    second.start()
    rows = _recaps(second)
    assert len(rows) == 1, f"seven days down produced {len(rows)} rows"
    assert rows[0]["day"] == "2024-01-03"
    assert rows[0]["days_skipped"] == 7
    second.stop()


def test_a_restart_inside_the_same_day_announces_nothing(tmp_path) -> None:
    """The positive control. Most restarts cross no boundary at all."""
    clock = [datetime(2024, 1, 3, 12, tzinfo=timezone.utc)]
    first = _engine(tmp_path, clock, cfg=_cfg(tmp_path))
    first.start()
    first.step_all()
    first.stop()

    clock[0] = datetime(2024, 1, 3, 23, 59, tzinfo=timezone.utc)
    second = _engine(tmp_path, clock, cfg=_cfg(tmp_path))
    second.start()
    assert _recaps(second) == [], "a same-day restart invented a recap"
    second.stop()


def test_a_first_ever_start_announces_nothing(tmp_path) -> None:
    """No snapshot, no observed day, nothing owed. The other control."""
    clock = [datetime(2024, 1, 3, 12, tzinfo=timezone.utc)]
    engine = _engine(tmp_path, clock, cfg=_cfg(tmp_path))
    engine.start()
    assert _recaps(engine) == []
    engine.stop()


def test_the_operator_sees_it_on_an_install_that_enumerates_notify_events(
    tmp_path,
) -> None:
    """It reaches chat, and that is why it is a `recap` row and not a new event.

    Every config.toml written before this change enumerates `notify_events`
    explicitly (`telegram.py`, ALWAYS_NOTIFY_EVENTS carries the same argument),
    so a new event name would have reached nobody on the live box: the operator
    who most needs this row is the one whose desk restarted. It is therefore
    the same `recap` event, with its own rendering.
    """
    clock = [datetime(2024, 1, 3, 12, tzinfo=timezone.utc)]
    first = _engine(tmp_path, clock, cfg=_cfg(tmp_path))
    first.start()
    first.step_all()
    first.stop()

    transport = FakeTransport()
    tg = TelegramClient(
        token="t", chat_id="1", notify_events=RECAP_NOTIFY, transport=transport
    )
    clock[0] = datetime(2024, 1, 4, 8, 0, tzinfo=timezone.utc)
    second = _engine(tmp_path, clock, telegram=tg, cfg=_cfg(tmp_path))
    second.start()
    texts = [
        p.get("text", "")
        for url, p in transport.sent
        if url.endswith("/sendMessage")
    ]
    recap_lines = [t for t in texts if t.startswith("RECAP")]
    assert len(recap_lines) == 1, f"the missed recap did not reach chat: {texts}"
    line = recap_lines[0]
    assert "2024-01-03" in line
    assert "NOT MEASURED" in line, (
        "the line reported a number for a day whose close was never observed: " + line
    )
    assert "pnl=+0" not in line, "a zero stood in for an unanswered question: " + line
    second.stop()
