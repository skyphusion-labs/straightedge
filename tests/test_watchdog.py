"""The heartbeat reader: three states, a derived threshold, and both driven RED.

`journal.heartbeat` was written on every tick since 1.0.0 and read by nothing,
which is the failure this repo keeps finding in a new costume: an instrument
that exists, looks official in the runbook, and cannot report the condition it
was installed for. These tests therefore do two things a smoke test would not.

**They drive the alarm red.** A stale heartbeat, a desk that came back DISARMED
after a restart, a halted desk, a file that predates the format, a file with no
timestamp and a desk with a wrong clock each produce their own named state and
their own exit code. A watchdog nobody has watched go red is not a watchdog.

**They separate the two states an up-or-down check would merge.** The same live
`Engine`, one `live_accepted` field apart, writes a heartbeat that reads
`ALIVE NOT TRADING (live_not_accepted)` and one that reads `ALIVE ARMED`. That
is the whole point: arming is per process on purpose (fc34), so a restart brings
the desk back ticking and refusing to trade, and an operator told only "it is
up" has been told the reassuring half of a two-state answer.

The numeric pins below are arithmetic over constants that live in
`straightedge.telegram` and the config sections, printed on every run. They are
here so that changing `RETRY_CAP_S` or a venue `timeout_ms` default cannot move
the alarm's latency silently: the number an operator was promised in `doctor`
and in the runbook changes in the same diff.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from straightedge import watchdog
from straightedge.broker.mt4_net import NET_GRACE_SEC
from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig, SessionConfig, TelegramConfig
from straightedge.engine import Engine
from straightedge.journal import InstanceLock
from straightedge.synthetic import generate_bars
from straightedge.telegram import POLL_TIMEOUT_MARGIN_S, RETRY_CAP_S, RETRY_TRIES, TelegramClient
from test_telegram import FakeTransport
from wincompat import assert_owner_mode

#: One long poll, retried to Telegram's own ceiling: RETRY_TRIES attempts at
#: (poll_seconds + POLL_TIMEOUT_MARGIN_S) each, with RETRY_TRIES - 1 gaps that a
#: `retry_after` can stretch to RETRY_CAP_S. With poll_seconds = 1, the value
#: the example config ships: 4 * 6 + 3 * 60.
CEILING_POLL_1 = 204.0
#: The MT4 steady-state budget is timeout_ms = 5000, and `step_all` spends two
#: commands (ensure_connected, account) before it can write the heartbeat.
MT4_PREFIX = 10.0
#: 204 + 10, doubled for the part of a tick no config value bounds.
MT4_BUDGET = 214
MT4_STALE = 428
#: MT5 ships timeout_ms = 60000, so the same desk shape gets a wider alarm. A
#: single constant could not have been right for both.
MT5_BUDGET = 324
MT5_STALE = 648


def test_the_alarm_derives_from_the_read_budget_and_not_the_send_budget() -> None:
    """A decision, pinned so it cannot be switched quietly.

    `venue_timeout_seconds` reads `mt4.timeout_ms`, the READ budget. The two
    commands `step_all` spends before it can write a heartbeat (`ensure_connected`
    and `account`) are both reads; a send lives in the part of a tick no config
    value bounds, which `UNBOUNDED_TAIL_ALLOWANCE` already doubles the budget for.
    Deriving the alarm from `mt4.send_timeout_ms` instead would widen the staleness
    threshold an operator was promised in `doctor` and in the runbook, for a cost
    the allowance already covers.

    The guard is that the budget must NOT move when only the send budget moves.
    """
    cfg = BotConfig()
    cfg.mode = "mt4"
    # `TelegramConfig.enabled` is derived from token and chat_id, both empty on a
    # default BotConfig, so the poll ceiling is already zero and the only term
    # left is the venue budget. With Telegram on, a 204s poll term would swamp a
    # few seconds of venue budget and this guard could not go red.
    assert not cfg.telegram.enabled
    assert cfg.mt4.send_timeout_ms != cfg.mt4.timeout_ms, (
        "this test cannot tell the two budgets apart, so it proves nothing"
    )
    before = watchdog.tick_budget_seconds(cfg)
    cfg.mt4.send_timeout_ms = cfg.mt4.send_timeout_ms * 4
    assert watchdog.tick_budget_seconds(cfg) == before, (
        "the alarm latency moved when only the SEND budget moved; the watchdog is "
        "now deriving from the wrong number and the promised staleness window in "
        "doctor and the runbook is wrong"
    )
    cfg.mt4.timeout_ms = cfg.mt4.timeout_ms * 2
    assert watchdog.tick_budget_seconds(cfg) > before, (
        "the alarm latency did NOT move when the READ budget moved, so this guard "
        "cannot go red"
    )


def _cfg(tmp_path: Path, *, mode: str = "paper", poll: int = 1, telegram: bool = True) -> BotConfig:
    cfg = BotConfig()
    cfg.mode = mode
    cfg.poll_seconds = poll
    cfg.session = SessionConfig(enabled=False)
    cfg.symbols = ["EURUSD"]
    cfg.journal_path = str(tmp_path / "journal.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    if telegram:
        cfg.telegram = TelegramConfig(token="t" * 10, chat_id="42")
    else:
        cfg.telegram = TelegramConfig()
    return cfg


class _RealMoneyPaper(PaperBroker):
    """A paper book that reports `trade_mode=2`.

    The un-stubbable seam this suite needs is the REAL `Engine`, the REAL risk
    gate and the REAL heartbeat file; the only thing faked is the one field that
    makes the account a real-money account, because CI has no live terminal.
    Everything downstream of it -- `circuit_reason`, `_write_heartbeat`,
    `watchdog.decide` -- is the shipped code path.
    """

    def account(self):  # type: ignore[no-untyped-def]
        return replace(super().account(), trade_mode=2)


def _engine(cfg: BotConfig, tmp_path: Path, *, real_money: bool = False) -> Engine:
    broker = _RealMoneyPaper(balance=10_000) if real_money else PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(80, drift=0.0004, seed=3))
    engine = Engine(cfg, broker, halt_dir=str(tmp_path))
    engine.start()
    return engine


# --- the derived threshold -------------------------------------------------------------


def test_tick_budget_is_derived_per_venue(tmp_path: Path) -> None:
    """The same desk shape gets a different alarm on each venue, from config."""
    mt4 = _cfg(tmp_path, mode="mt4")
    mt5 = _cfg(tmp_path, mode="mt5")
    print(
        f"watchdog: telegram ceiling at poll_seconds=1 = "
        f"{watchdog.telegram_poll_ceiling_seconds(mt4)}/{CEILING_POLL_1}"
    )
    assert watchdog.telegram_poll_ceiling_seconds(mt4) == CEILING_POLL_1
    assert RETRY_TRIES * (1 + POLL_TIMEOUT_MARGIN_S) + (RETRY_TRIES - 1) * RETRY_CAP_S == (
        CEILING_POLL_1
    )
    assert watchdog.venue_timeout_seconds(mt4) * watchdog.VENUE_CALLS_BEFORE_HEARTBEAT == (
        MT4_PREFIX
    )
    print(f"watchdog: mt4 budget {watchdog.tick_budget_seconds(mt4)}/{MT4_BUDGET}")
    print(f"watchdog: mt5 budget {watchdog.tick_budget_seconds(mt5)}/{MT5_BUDGET}")
    assert watchdog.tick_budget_seconds(mt4) == MT4_BUDGET
    assert watchdog.stale_after_seconds(mt4) == MT4_STALE
    assert watchdog.tick_budget_seconds(mt5) == MT5_BUDGET
    assert watchdog.stale_after_seconds(mt5) == MT5_STALE


def test_network_transport_widens_the_budget_by_its_own_grace(tmp_path: Path) -> None:
    """A remote shim adds `NET_GRACE_SEC` per command, so the alarm follows it."""
    cfg = _cfg(tmp_path, mode="mt4")
    cfg.mt4.mailbox_url = "http://127.0.0.1:8730/"
    expected = watchdog.VENUE_CALLS_BEFORE_HEARTBEAT * (5.0 + NET_GRACE_SEC)
    assert watchdog.venue_timeout_seconds(cfg) * 2 == expected
    assert watchdog.tick_budget_seconds(cfg) > MT4_BUDGET


def test_a_quiet_paper_desk_gets_no_venue_term(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, telegram=False)
    assert watchdog.venue_timeout_seconds(cfg) == 0.0
    assert watchdog.telegram_poll_ceiling_seconds(cfg) == 0.0
    # Never zero: a threshold of zero seconds alarms on every healthy desk.
    assert watchdog.tick_budget_seconds(cfg) == 1


def test_poll_seconds_moves_the_threshold(tmp_path: Path) -> None:
    """A 15 second long poll cannot be judged by a 1 second desk's threshold."""
    fast = watchdog.stale_after_seconds(_cfg(tmp_path, mode="mt4", poll=1))
    slow = watchdog.stale_after_seconds(_cfg(tmp_path, mode="mt4", poll=15))
    print(f"watchdog: stale_after poll=1 {fast}s vs poll=15 {slow}s")
    assert slow > fast


# --- the three states, from a live Engine ----------------------------------------------


def test_disarmed_after_restart_is_not_reported_healthy(tmp_path: Path) -> None:
    """State 2. The desk ticks, refuses real money, and SAYS which one it is."""
    cfg = _cfg(tmp_path, mode="mt4")
    engine = _engine(cfg, tmp_path, real_money=True)
    engine.step_all()
    engine.stop()
    path = watchdog.heartbeat_path_for(cfg.journal_path)
    assert path.is_file()
    assert_owner_mode(path)
    report = watchdog.decide(path, now=datetime.now(timezone.utc), cfg=cfg)
    print(f"watchdog: disarmed desk -> {report.state} ({report.reason})")
    assert report.state == watchdog.STATE_NOT_TRADING
    assert report.reason == "live_not_accepted"
    assert report.exit_code == watchdog.EXIT_NOT_TRADING
    assert "I-ACCEPT-RISK" in report.text
    # The remedy is the chat command, never a flag on the scheduled task.
    assert "--i-accept-risk" in report.text and "do NOT" in report.text


def test_armed_desk_reads_armed(tmp_path: Path) -> None:
    """State 3. One field apart from the test above, and a different answer."""
    cfg = _cfg(tmp_path, mode="mt4")
    cfg.live_accepted = True
    engine = _engine(cfg, tmp_path, real_money=True)
    engine.step_all()
    engine.stop()
    path = watchdog.heartbeat_path_for(cfg.journal_path)
    report = watchdog.decide(path, now=datetime.now(timezone.utc), cfg=cfg)
    print(f"watchdog: armed desk -> {report.state}")
    assert report.state == watchdog.STATE_ARMED
    assert report.exit_code == watchdog.EXIT_ARMED
    assert report.ok


def test_a_halted_desk_is_ticking_and_not_trading(tmp_path: Path) -> None:
    """The fourth state the three-state framing leaves out, named not merged."""
    cfg = _cfg(tmp_path, mode="mt4")
    cfg.live_accepted = True
    engine = _engine(cfg, tmp_path, real_money=True)
    Path(cfg.risk.halt_file).write_text("halt\n", encoding="utf-8")
    engine.step_all()
    engine.stop()
    path = watchdog.heartbeat_path_for(cfg.journal_path)
    report = watchdog.decide(path, now=datetime.now(timezone.utc), cfg=cfg)
    print(f"watchdog: halted desk -> {report.state} ({report.reason})")
    assert report.state == watchdog.STATE_NOT_TRADING
    assert report.reason == "halt_file"
    assert report.exit_code == watchdog.EXIT_NOT_TRADING


def test_stale_heartbeat_drives_the_alarm_red(tmp_path: Path) -> None:
    """State 1. The same healthy file, judged one threshold later."""
    cfg = _cfg(tmp_path, mode="mt4")
    cfg.live_accepted = True
    engine = _engine(cfg, tmp_path, real_money=True)
    engine.step_all()
    engine.stop()
    path = watchdog.heartbeat_path_for(cfg.journal_path)
    fresh = watchdog.decide(path, now=datetime.now(timezone.utc), cfg=cfg)
    assert fresh.state == watchdog.STATE_ARMED
    later = datetime.now(timezone.utc) + timedelta(seconds=MT4_STALE + 1)
    report = watchdog.decide(path, now=later, cfg=cfg)
    print(f"watchdog: {MT4_STALE + 1}s later -> {report.state}")
    assert report.state == watchdog.STATE_STALE
    assert report.exit_code == watchdog.EXIT_STALE
    # Named honestly: this check cannot see the process, only the file.
    assert "STALE" in report.text and "reconnect" in report.text


#: A heartbeat that says the journal has stopped rotating, hand written for the
#: same reason every other minimal heartbeat in this file is: these cases are
#: about `decide`, and the REAL path (a live `Engine`, a refused rotation, the
#: real heartbeat, and the alert clearing when the holder lets go) is driven
#: end to end in `tests/test_the_replace_idiom_is_guarded_everywhere.py`.
def _hb(path: Path, *, now: datetime, blocked: str = "", **fields: object) -> None:
    lines = [now.isoformat(), f"blocked={blocked}", f"stale_after_s={MT4_STALE}"]
    lines += [f"{k}={v}" for k, v in fields.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_a_journal_over_its_bound_is_an_alert_and_PAGES(tmp_path: Path) -> None:
    """straightedge#288. The condition reaches the operator's own channel.

    Not `decide` alone: the finding was arriving-but-unwatched, so the case
    has to end at the thing that pages. `watch` is the reader, Telegram is the
    channel, and the exit code is what a scheduled task sees.
    """
    cfg = _cfg(tmp_path, mode="mt4")
    path = watchdog.heartbeat_path_for(cfg.journal_path)
    now = datetime.now(timezone.utc)
    _hb(
        path,
        now=now,
        journal_bytes=11_000_000,
        rotate_bytes=10_485_760,
        rotate_deferrals=37,
    )
    report = watchdog.decide(path, now=now, cfg=cfg)
    print(f"watchdog: unrotated journal -> {report.state} ({report.reason})")
    assert report.state == watchdog.STATE_DEGRADED
    assert report.reason == "rotation_stuck"
    assert report.exit_code == watchdog.EXIT_DEGRADED
    assert not report.ok, (
        "an `ok` report is told once and never repeated, which is the defect "
        "this change exists to close"
    )
    assert "514240 over" in report.text, report.text
    assert "rotate_deferrals=37" in report.text, report.text

    client, transport = _client()
    code = watchdog.watch(path, cfg, send=client.send, out=lambda line: None)
    sent = [body for _, body in transport.sent]
    assert code == watchdog.EXIT_DEGRADED
    assert sent, "the watcher sent nothing, so this proves nothing"
    assert any("STOPPED ROTATING" in str(body) for body in sent), sent


def test_a_rotation_failing_for_NO_NAMED_REASON_still_alerts(tmp_path: Path) -> None:
    """The more general instrument, which is why the count is not the trigger.

    `rotate_deferrals=0` with the file over its bound means the rotation is
    not being REFUSED by a holder: it is failing, or not being attempted, for
    something nobody has thought of. An alert keyed on the deferral count
    would read that as healthy, which is the one case an alert on the cause
    structurally cannot see.
    """
    cfg = _cfg(tmp_path, mode="mt4")
    path = watchdog.heartbeat_path_for(cfg.journal_path)
    now = datetime.now(timezone.utc)
    _hb(path, now=now, journal_bytes=10_485_761, rotate_bytes=10_485_760,
        rotate_deferrals=0)
    report = watchdog.decide(path, now=now, cfg=cfg)
    assert report.state == watchdog.STATE_DEGRADED
    assert "does not have a name for" in report.text, report.text


def test_a_transient_holder_that_let_go_is_NOT_an_alert(tmp_path: Path) -> None:
    """The false positive this design is shaped to avoid.

    A backup pass that held the journal across one rotation leaves
    `rotate_deferrals` at 1 for the life of the process, with nothing wrong
    and nothing for an operator to do. Any threshold on that count fires here
    and never clears, which is how an alarm gets muted. The file is inside its
    bound, so the rotation IS happening, so this is a note and not a state.
    """
    cfg = _cfg(tmp_path, mode="mt4")
    path = watchdog.heartbeat_path_for(cfg.journal_path)
    now = datetime.now(timezone.utc)
    _hb(path, now=now, journal_bytes=4_096, rotate_bytes=10_485_760,
        rotate_deferrals=1)
    report = watchdog.decide(path, now=now, cfg=cfg)
    print(f"watchdog: a holder that let go -> {report.state}")
    assert report.state == watchdog.STATE_ARMED
    assert report.exit_code == watchdog.EXIT_ARMED
    assert report.ok
    assert "let go" in report.text, report.text


def test_the_rotation_READING_ABSENT_is_not_read_as_healthy(tmp_path: Path) -> None:
    """A desk too old to publish the pair is UNMEASURED, not fine.

    The same rule the file already applies to `over_budget_ever` and `run_id`:
    an absent field is the absence of a measurement. `rotate_deferrals` on its
    own cannot answer this, and saying so is the difference between a watcher
    that does not know and one that reports a desk it cannot see as healthy.
    """
    cfg = _cfg(tmp_path, mode="mt4")
    path = watchdog.heartbeat_path_for(cfg.journal_path)
    now = datetime.now(timezone.utc)
    _hb(path, now=now, rotate_deferrals=0)
    report = watchdog.decide(path, now=now, cfg=cfg)
    assert report.state == watchdog.STATE_ARMED, report.text
    assert "UNMEASURED" in report.text, report.text
    assert "NOT read as a healthy rotation" in report.text, report.text

    # A bound of zero is the dangerous half of the pair: judged as a number it
    # would make every nonzero size an alert.
    _hb(path, now=now, journal_bytes=4_096, rotate_bytes=0, rotate_deferrals=0)
    zero = watchdog.decide(path, now=now, cfg=cfg)
    assert zero.state == watchdog.STATE_ARMED, zero.text
    assert "UNMEASURED" in zero.text, zero.text


def test_a_desk_that_WILL_NOT_TRADE_is_the_louder_condition(tmp_path: Path) -> None:
    """Precedence, asserted rather than left to the order of two `if`s.

    A halted desk with a stuck journal reports the halt, because the halt is
    the urgent remedy. The rotation reading is not lost: it rides that report
    as a note, and a NOT_TRADING report is already not `ok`, so `watch`
    repeats it on the desk's own threshold and the note repeats with it. The
    only state this can hide behind is one that is already being told.
    """
    cfg = _cfg(tmp_path, mode="mt4")
    path = watchdog.heartbeat_path_for(cfg.journal_path)
    now = datetime.now(timezone.utc)
    _hb(path, now=now, blocked="daily_loss", journal_bytes=11_000_000,
        rotate_bytes=10_485_760, rotate_deferrals=4)
    report = watchdog.decide(path, now=now, cfg=cfg)
    assert report.state == watchdog.STATE_NOT_TRADING
    assert report.reason == "daily_loss"
    assert not report.ok
    assert "rotation bound" in report.text, (
        "the halt hid the rotation reading entirely: " + report.text
    )

    # And a STALE desk, where the figures are stale too and the note says the
    # reading, not a verdict.
    later = now + timedelta(seconds=MT4_STALE + 1)
    stale = watchdog.decide(path, now=later, cfg=cfg)
    assert stale.state == watchdog.STATE_STALE
    assert "rotation bound" in stale.text, stale.text


def test_one_second_before_the_threshold_is_still_alive(tmp_path: Path) -> None:
    """The gate is not simply always red: the boundary is the published one."""
    cfg = _cfg(tmp_path, mode="mt4")
    cfg.live_accepted = True
    engine = _engine(cfg, tmp_path, real_money=True)
    engine.step_all()
    engine.stop()
    path = watchdog.heartbeat_path_for(cfg.journal_path)
    just_inside = datetime.now(timezone.utc) + timedelta(seconds=MT4_STALE - 5)
    assert watchdog.decide(path, now=just_inside, cfg=cfg).state == watchdog.STATE_ARMED


# --- what the reader refuses to guess --------------------------------------------------


def test_no_heartbeat_file_is_unknown_not_dead(tmp_path: Path) -> None:
    """A missing file is also a watcher pointed at the wrong path."""
    cfg = _cfg(tmp_path)
    report = watchdog.decide(tmp_path / "nope.heartbeat", now=datetime.now(timezone.utc), cfg=cfg)
    assert report.state == watchdog.STATE_UNKNOWN
    assert report.reason == "no_heartbeat"
    assert report.exit_code == watchdog.EXIT_UNKNOWN
    assert "--config" in report.text


def test_a_heartbeat_with_no_threshold_refuses_to_be_judged(tmp_path: Path) -> None:
    """The old format: an ISO timestamp alone. Derive or refuse (#68)."""
    path = tmp_path / "journal.heartbeat"
    path.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")
    report = watchdog.decide(path, now=datetime.now(timezone.utc), cfg=_cfg(tmp_path))
    assert report.state == watchdog.STATE_UNKNOWN
    assert report.reason == "no_threshold"
    assert "will not invent one" in report.text


def test_a_heartbeat_with_no_gate_state_is_not_called_armed(tmp_path: Path) -> None:
    path = tmp_path / "journal.heartbeat"
    path.write_text(
        datetime.now(timezone.utc).isoformat() + "\nstale_after_s=400\n", encoding="utf-8"
    )
    report = watchdog.decide(path, now=datetime.now(timezone.utc), cfg=_cfg(tmp_path))
    assert report.state == watchdog.STATE_UNKNOWN
    assert report.reason == "no_gate_state"


def test_an_unparseable_first_line_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "journal.heartbeat"
    path.write_text("not a timestamp\nblocked=\nstale_after_s=400\n", encoding="utf-8")
    report = watchdog.decide(path, now=datetime.now(timezone.utc), cfg=_cfg(tmp_path))
    assert report.state == watchdog.STATE_UNKNOWN
    assert report.reason == "unparseable"


def test_a_wrong_desk_clock_reads_as_stale_not_as_healthy(tmp_path: Path) -> None:
    """Age is taken from the stamp AND the filesystem, whichever is worse.

    A desk whose clock is wrong is not a healthy desk: `day_key` sets the
    daily-loss budget from that clock. So the two readings are not averaged and
    the reassuring one is not preferred.
    """
    path = tmp_path / "journal.heartbeat"
    old = datetime.now(timezone.utc) - timedelta(seconds=MT4_STALE * 3)
    path.write_text(f"{old.isoformat()}\nblocked=\nstale_after_s={MT4_STALE}\n", encoding="utf-8")
    report = watchdog.decide(path, now=datetime.now(timezone.utc), cfg=_cfg(tmp_path, mode="mt4"))
    assert report.state == watchdog.STATE_STALE


def test_a_config_mismatch_is_reported_not_resolved(tmp_path: Path) -> None:
    """Two configs in play is a real install fault, and it is named."""
    path = tmp_path / "journal.heartbeat"
    now = datetime.now(timezone.utc)
    path.write_text(f"{now.isoformat()}\nblocked=\nstale_after_s=9999\n", encoding="utf-8")
    report = watchdog.decide(path, now=now, cfg=_cfg(tmp_path, mode="mt4"))
    assert report.state == watchdog.STATE_ARMED
    assert "9999s" in report.text and "428s" in report.text


def test_exit_codes_are_distinct() -> None:
    """A caller that can only see 0 or 1 is back to up-or-down."""
    codes = {
        watchdog.EXIT_ARMED,
        watchdog.EXIT_NOT_TRADING,
        watchdog.EXIT_STALE,
        watchdog.EXIT_UNKNOWN,
    }
    print(f"watchdog: distinct exit codes {sorted(codes)}/4")
    assert len(codes) == 4


# --- the file format ------------------------------------------------------------------


def test_first_line_is_still_a_bare_iso_timestamp(tmp_path: Path) -> None:
    """The 1.0.0 contract. Every older reader and doc keeps working."""
    cfg = _cfg(tmp_path)
    engine = _engine(cfg, tmp_path)
    engine.step_all()
    engine.stop()
    text = watchdog.heartbeat_path_for(cfg.journal_path).read_text(encoding="utf-8")
    first = text.splitlines()[0]
    datetime.fromisoformat(first)
    assert "=" not in first
    assert "blocked=" in text and "stale_after_s=" in text


def test_a_gap_over_budget_is_reported_and_the_threshold_is_not_widened(
    tmp_path: Path, monkeypatch
) -> None:
    """The allowance is checked against reality, not trusted.

    The doubling in `stale_after_seconds` covers the part of a tick no config
    value bounds, so it is the one term here that is not a measurement. A desk
    that overruns it says so; it must NOT quietly widen its own alarm, because a
    gate that relaxes itself until it stops firing cannot go red any more.
    """
    import straightedge.engine as engine_mod

    cfg = _cfg(tmp_path, mode="mt4")
    engine = _engine(cfg, tmp_path)
    clock = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])
    engine.step_all()
    clock[0] = float(MT4_BUDGET) * 10
    engine.step_all()
    engine.stop()
    text = watchdog.heartbeat_path_for(cfg.journal_path).read_text(encoding="utf-8")
    assert "over_budget=1" in text
    assert f"stale_after_s={MT4_STALE}" in text
    report = watchdog.decide(
        watchdog.heartbeat_path_for(cfg.journal_path),
        now=datetime.now(timezone.utc),
        cfg=cfg,
    )
    assert "NOT widened" in report.text


# --- the watch loop -------------------------------------------------------------------


def _client() -> tuple[TelegramClient, FakeTransport]:
    transport = FakeTransport()
    client = TelegramClient(token="t" * 10, chat_id="42", transport=transport)
    return client, transport


def test_watch_never_calls_getupdates(tmp_path: Path) -> None:
    """Two processes polling one bot token steal each other's commands.

    `docs/RUNBOOK.md` forbids two loops for exactly this reason, and a watcher
    beside a desk is the obvious way to reintroduce it. The guard is this
    assertion, not the intention of the author.
    """
    cfg = _cfg(tmp_path, mode="mt4")
    engine = _engine(cfg, tmp_path, real_money=True)
    engine.step_all()
    engine.stop()
    client, transport = _client()
    code = watchdog.watch(
        watchdog.heartbeat_path_for(cfg.journal_path),
        cfg,
        send=client.send,
        out=lambda line: None,
        loop=True,
        sleep_fn=lambda seconds: None,
        max_checks=3,
    )
    urls = [url for url, _ in transport.sent]
    print(f"watchdog: {len(urls)} telegram calls, getUpdates in {sum('getUpdates' in u for u in urls)}")
    assert urls, "the watcher sent nothing at all, so this proves nothing"
    assert not any("getUpdates" in url for url in urls)
    assert all(url.endswith("/sendMessage") for url in urls)
    assert code == watchdog.EXIT_NOT_TRADING


def test_watch_tells_the_operator_once_then_on_change(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, mode="mt4")
    engine = _engine(cfg, tmp_path, real_money=True)
    engine.step_all()
    client, transport = _client()
    path = watchdog.heartbeat_path_for(cfg.journal_path)
    watchdog.watch(path, cfg, send=client.send, out=lambda line: None, loop=True,
                   sleep_fn=lambda seconds: None, max_checks=4)
    first = len(transport.sent)
    assert first == 1, f"expected one alert for one unchanging state, got {first}"
    # Now arm it: the state changes, so the operator hears about it again.
    cfg.live_accepted = True
    engine.step_all()
    engine.stop()
    watchdog.watch(path, cfg, send=client.send, out=lambda line: None, loop=True,
                   sleep_fn=lambda seconds: None, max_checks=2)
    assert len(transport.sent) == first + 1
    assert "ALIVE ARMED" in transport.sent[-1][1]["text"]


def test_watch_sleeps_to_the_published_deadline(tmp_path: Path) -> None:
    """No polling interval anybody picked: the desk's own deadline is the wait."""
    cfg = _cfg(tmp_path, mode="mt4")
    engine = _engine(cfg, tmp_path)
    engine.step_all()
    engine.stop()
    slept: list[float] = []
    watchdog.watch(
        watchdog.heartbeat_path_for(cfg.journal_path),
        cfg,
        out=lambda line: None,
        loop=True,
        sleep_fn=slept.append,
        max_checks=2,
    )
    assert slept and slept[0] <= MT4_STALE
    assert slept[0] > MT4_STALE - 30


def test_watch_ok_beat_makes_silence_a_signal(tmp_path: Path) -> None:
    """A dead watcher is silent, and nothing on the box can observe that.

    With `ok_every` set, a healthy desk is confirmed on a cadence, so the
    operator's absence of messages becomes evidence rather than the default.
    Driven on an injected clock: a cadence rule timed by real elapsed
    microseconds is a rule the test cannot actually hold to account.
    """
    cfg = _cfg(tmp_path, mode="mt4")
    cfg.live_accepted = True
    engine = _engine(cfg, tmp_path, real_money=True)
    engine.step_all()
    engine.stop()
    path = watchdog.heartbeat_path_for(cfg.journal_path)

    def _clock(step: int):
        base = datetime.now(timezone.utc)
        ticks = [0]

        def _now() -> datetime:
            value = base + timedelta(seconds=step * ticks[0])
            ticks[0] += 1
            return value

        return _now

    client, transport = _client()
    watchdog.watch(path, cfg, send=client.send, out=lambda line: None, loop=True,
                   sleep_fn=lambda seconds: None, now_fn=_clock(10), ok_every=0.0,
                   max_checks=4)
    quiet = len(transport.sent)
    print(f"watchdog: ok_every off -> {quiet} alert(s) over 4 checks")
    assert quiet == 1

    client, transport = _client()
    watchdog.watch(path, cfg, send=client.send, out=lambda line: None, loop=True,
                   sleep_fn=lambda seconds: None, now_fn=_clock(10), ok_every=10.0,
                   max_checks=4)
    beats = len(transport.sent)
    print(f"watchdog: ok_every=10s on a 10s clock -> {beats} alert(s) over 4 checks")
    assert beats == 4


def test_watch_reports_a_failed_send_instead_of_swallowing_it(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, mode="mt4")
    engine = _engine(cfg, tmp_path)
    engine.step_all()
    engine.stop()
    client, transport = _client()
    transport.fail = True
    lines: list[str] = []
    watchdog.watch(
        watchdog.heartbeat_path_for(cfg.journal_path),
        cfg,
        send=client.send,
        out=lines.append,
        loop=False,
    )
    assert any("Telegram send FAILED" in line for line in lines)


def test_watch_does_not_touch_the_run_lock(tmp_path: Path) -> None:
    """A watchdog that can hold `journal.lock` can make the desk exit 2.

    The desk's second-instance guard is an exclusive lock, so a watcher that
    acquired it for even a moment could make the scheduled task's restart fail
    with `already running`. This drives the real seam: the lock is HELD for the
    whole check, exactly as it is while a desk runs.
    """
    cfg = _cfg(tmp_path, mode="mt4")
    engine = _engine(cfg, tmp_path)
    engine.step_all()
    engine.stop()
    lock = InstanceLock(cfg.journal_path)
    lock.acquire()
    try:
        code = watchdog.watch(
            watchdog.heartbeat_path_for(cfg.journal_path),
            cfg,
            out=lambda line: None,
            loop=False,
        )
    finally:
        lock.release()
    assert code == watchdog.EXIT_ARMED


def test_heartbeat_path_sits_next_to_the_journal(tmp_path: Path) -> None:
    path = watchdog.heartbeat_path_for(tmp_path / "journal.jsonl")
    assert path == tmp_path / "journal.heartbeat"


@pytest.mark.parametrize("blocked", ["daily_loss", "max_drawdown", "state_unwritable"])
def test_every_circuit_reason_survives_the_round_trip(tmp_path: Path, blocked: str) -> None:
    """The reader does not carry a list of reasons it recognises.

    `circuit_reason` can grow a new refusal and this must report it by name
    rather than fall back to "not trading, cause unknown", which is how a
    reason nobody enumerated becomes invisible.
    """
    path = tmp_path / "journal.heartbeat"
    now = datetime.now(timezone.utc)
    path.write_text(
        f"{now.isoformat()}\nblocked={blocked}\nstale_after_s=428\n", encoding="utf-8"
    )
    report = watchdog.decide(path, now=now, cfg=_cfg(tmp_path, mode="mt4"))
    assert report.state == watchdog.STATE_NOT_TRADING
    assert report.reason == blocked
    assert blocked in report.text


# --- the parser's own edges ------------------------------------------------------------


def test_an_empty_heartbeat_is_unknown(tmp_path: Path) -> None:
    """A zero-byte file is what a truncated write leaves behind."""
    path = tmp_path / "journal.heartbeat"
    path.write_text("", encoding="utf-8")
    report = watchdog.decide(path, now=datetime.now(timezone.utc), cfg=_cfg(tmp_path))
    assert report.state == watchdog.STATE_UNKNOWN
    assert report.reason == "unparseable"


def test_a_naive_timestamp_is_read_as_utc_never_as_local(tmp_path: Path) -> None:
    """A local-time reading would move the age by the host's UTC offset.

    Conrad's own seat is US Central, so reading a naive stamp as local time
    would make a healthy desk look five or six hours stale, or a dead one look
    fresh, depending on the sign.
    """
    path = tmp_path / "journal.heartbeat"
    naive = datetime.now(timezone.utc).replace(tzinfo=None)
    path.write_text(f"{naive.isoformat()}\nblocked=\nstale_after_s=428\n", encoding="utf-8")
    hb = watchdog.read(path)
    assert hb is not None and hb.ts is not None and hb.ts.tzinfo is timezone.utc
    report = watchdog.decide(path, now=datetime.now(timezone.utc), cfg=_cfg(tmp_path, mode="mt4"))
    assert report.state == watchdog.STATE_ARMED


def test_a_junk_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "journal.heartbeat"
    now = datetime.now(timezone.utc)
    path.write_text(
        f"{now.isoformat()}\nthis line has no equals sign\nblocked=\nstale_after_s=428\n",
        encoding="utf-8",
    )
    assert watchdog.decide(path, now=now, cfg=_cfg(tmp_path, mode="mt4")).state == (
        watchdog.STATE_ARMED
    )


def test_a_non_numeric_threshold_is_refused_not_coerced(tmp_path: Path) -> None:
    path = tmp_path / "journal.heartbeat"
    now = datetime.now(timezone.utc)
    path.write_text(f"{now.isoformat()}\nblocked=\nstale_after_s=soon\n", encoding="utf-8")
    report = watchdog.decide(path, now=now, cfg=_cfg(tmp_path))
    assert report.state == watchdog.STATE_UNKNOWN
    assert report.reason == "no_threshold"


def test_decide_works_with_no_config_at_all(tmp_path: Path) -> None:
    """The desk's published threshold is enough; the config is a cross-check."""
    path = tmp_path / "journal.heartbeat"
    now = datetime.now(timezone.utc)
    path.write_text(f"{now.isoformat()}\nblocked=\nstale_after_s=428\n", encoding="utf-8")
    report = watchdog.decide(path, now=now)
    assert report.state == watchdog.STATE_ARMED
    # Narrowed from `"NOTE:" not in text` when run_id landed. What this test is
    # about is that passing no config costs you no CONFIG-MISMATCH note, and
    # that note is the one naming a second config. The hand-written heartbeat
    # above carries no run_id, so it now also earns the run_id note, which is
    # correct and is asserted by its own test; a blanket "no notes at all" here
    # would have made this test fail for a reason it never claimed to cover.
    assert "two different configs" not in report.text.lower()
    assert "derives" not in report.text
    missing = watchdog.decide(tmp_path / "gone.heartbeat", now=now)
    assert missing.state == watchdog.STATE_UNKNOWN


def test_doctor_line_does_not_print_a_number_a_running_desk_cannot_get(
    tmp_path: Path,
) -> None:
    """`doctor` states the threshold, and says when the figure is incomplete.

    With no Telegram token the long poll is absent, so the derived budget
    collapses to its floor. `run` refuses to start without Telegram at all, so
    printing that bare figure would describe a desk that cannot exist.
    """
    from straightedge.__main__ import watchdog_line

    quiet = watchdog_line(_cfg(tmp_path, mode="mt4", telegram=False))
    live = watchdog_line(_cfg(tmp_path, mode="mt4"))
    print(f"watchdog: doctor line, telegram unset -> {quiet}")
    print(f"watchdog: doctor line, telegram set   -> {live}")
    assert "telegram unset" in quiet
    assert "telegram unset" not in live
    assert f"stale after {MT4_STALE}s" in live


# --- the restart itself, which used to be invisible ------------------------------------
#
# straightedge#133 requirement 3: a restart is reported, not silent. Before
# `run_id`, the only trace a restart left in the heartbeat was the arming state
# falling back to `live_not_accepted`, and that state exists ONLY on a
# real-money desk. `test_disarmed_after_restart_is_not_reported_healthy` above
# covers that path, and it is the reason this gap survived review: it reads as
# full coverage of "a restart is visible". On a demo account nothing needs
# arming, so every field read the same either side of a crash and the restart
# was silent in exactly the configuration an end user is shown. These tests are
# the demo-account half.


def _engine_at(cfg: BotConfig, tmp_path: Path, clock: Any) -> Engine:
    """`_engine`, with the process clock injected.

    A separate builder rather than a parameter on `_engine` because the clock
    has to be in place for `start()` as well as for the ticks: an engine whose
    `start()` ran on the wall clock and whose heartbeat ran on a driven one
    would be measuring a desk whose own day boundary moved under it.
    """
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(80, drift=0.0004, seed=3))
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=clock)
    engine.start()
    return engine


def _published_run_id(cfg: BotConfig) -> str:
    """What the heartbeat FILE says, never what the object remembers.

    The contract under test is the published one: `watch` is a separate process
    and the file is all it ever gets.
    """
    hb = watchdog.read(watchdog.heartbeat_path_for(cfg.journal_path))
    assert hb is not None, "no heartbeat was written at all, so this proves nothing"
    return hb.fields.get("run_id", "")


def test_the_heartbeat_names_the_process_that_wrote_it(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, mode="mt4")
    first_engine = _engine(cfg, tmp_path)
    first_engine.step_all()
    first_engine.stop()
    first = _published_run_id(cfg)
    started = watchdog.read(watchdog.heartbeat_path_for(cfg.journal_path))
    assert started is not None and started.fields.get("started_at")
    second_engine = _engine(cfg, tmp_path)
    second_engine.step_all()
    second_engine.stop()
    second = _published_run_id(cfg)
    print(f"watchdog: run_id {first} -> {second}")
    assert first and second
    assert second != first, (
        "two desk processes published the same identity, so no restart between "
        "them could ever be detected"
    )


def test_one_process_keeps_its_identity_across_ticks(tmp_path: Path) -> None:
    """Otherwise every tick reads as a restart, and an alarm that fires every
    tick is one an operator mutes."""
    cfg = _cfg(tmp_path, mode="mt4")
    engine = _engine(cfg, tmp_path)
    engine.step_all()
    first = _published_run_id(cfg)
    engine.step_all()
    engine.stop()
    assert _published_run_id(cfg) == first


def _watch_across_restarts(
    cfg: BotConfig,
    tmp_path: Path,
    *,
    restarts: int = 1,
    gap_s: float = 5.0,
) -> tuple[list[str], int]:
    """Drive ONE `watch` loop over `restarts + 1` desk processes.

    The replacement desk ticks from inside `sleep_fn`, which is the only hook
    the loop offers for "the world changed between two checks", and the clock
    the whole arrangement shares is driven by the same function. `gap_s` is
    what lets a test choose whether the previous run lived long enough to be a
    restart or briefly enough to be a crash loop.
    """
    client, transport = _client()
    path = watchdog.heartbeat_path_for(cfg.journal_path)
    now = [datetime.now(timezone.utc)]
    done = [0]

    def clock() -> datetime:
        return now[0]


    first = _engine_at(cfg, tmp_path, clock)
    first.step_all()
    first.stop()

    def advance(_seconds: float) -> None:
        if done[0] >= restarts:
            return
        done[0] += 1
        now[0] = now[0] + timedelta(seconds=gap_s)
        engine = _engine_at(cfg, tmp_path, clock)
        engine.step_all()
        engine.stop()

    code = watchdog.watch(
        path,
        cfg,
        send=client.send,
        out=lambda line: None,
        now_fn=clock,
        loop=True,
        sleep_fn=advance,
        max_checks=restarts + 1,
    )
    return [payload["text"] for _url, payload in transport.sent], code


def test_a_restart_that_changes_no_state_is_still_announced(tmp_path: Path) -> None:
    """THE gap: a demo desk, ARMED before and ARMED after, must still tell.

    Without the process identity in the heartbeat both observations are
    identical apart from the timestamp, `Report.key` is the same tuple, and
    `watch` says nothing the second time. Deleting the two `run_id` lines from
    `watchdog.render` drives this red, which is how it was checked.
    """
    cfg = _cfg(tmp_path, mode="mt4")
    texts, code = _watch_across_restarts(cfg, tmp_path)
    print(f"watchdog: {len(texts)} alerts across one restart, exit {code}")
    assert len(texts) == 2, (
        f"a restart produced {len(texts)} alert(s). ARMED -> ARMED is not a "
        "change of state, so with no process identity this is 1"
    )
    assert "ALIVE ARMED" in texts[0]
    assert "RESTARTED" not in texts[0], "a first sighting is not a restart"
    assert "RESTARTED" in texts[1]
    assert "ALIVE ARMED" in texts[1], "the restart line must not displace the state"
    assert "restart 1" in texts[1]
    assert "I-ACCEPT-RISK" in texts[1], "a restarted real-money desk is disarmed"


def test_a_crash_loop_is_named_as_one_and_counted(tmp_path: Path) -> None:
    """Requirement 3's other half: a supervisor must not quieten a crash loop.

    One restart overnight is information. The same line reading `restart 3` is
    a different fact, and an operator should not have to scroll a chat to tell
    them apart.
    """
    cfg = _cfg(tmp_path, mode="mt4")
    texts, _code = _watch_across_restarts(cfg, tmp_path, restarts=3, gap_s=5.0)
    assert len(texts) == 4
    assert "restart 1" in texts[1]
    assert "restart 3" in texts[3], (
        "the count does not rise, so three crashes read as one event"
    )
    assert "CRASH LOOP" in texts[3]
    assert "at most 5s" in texts[3]


def test_a_restart_after_a_long_healthy_run_is_not_called_a_loop(tmp_path: Path) -> None:
    """The positive control for the line above: it has to be able NOT to fire.

    A claim made on every restart carries no information. This gap is longer
    than the derived staleness threshold, so even the UPPER bound on the
    previous run's life clears it.
    """
    cfg = _cfg(tmp_path, mode="mt4")
    texts, _code = _watch_across_restarts(cfg, tmp_path, gap_s=MT4_STALE + 60)
    assert "RESTARTED" in texts[1]
    assert "CRASH LOOP" not in texts[1]


def test_a_desk_with_no_run_id_never_reads_as_restarting_and_says_why(
    tmp_path: Path,
) -> None:
    """The live box runs a desk that predates this field, so this is not
    hypothetical.

    An absent field must not read as an unchanged one, and must not read as a
    restart either. It reads as "this cannot be measured here", in the report,
    where an operator sees it.
    """
    path = tmp_path / "journal.heartbeat"
    cfg = _cfg(tmp_path, mode="mt4")
    client, transport = _client()
    now = [datetime.now(timezone.utc)]

    def rewrite(_seconds: float) -> None:
        now[0] = now[0] + timedelta(seconds=5)
        path.write_text(
            f"{now[0].isoformat()}\nblocked=\nmode=mt4\nstale_after_s={MT4_STALE}\n",
            encoding="utf-8",
        )

    rewrite(0.0)
    code = watchdog.watch(
        path,
        cfg,
        send=client.send,
        out=lambda line: None,
        now_fn=lambda: now[0],
        loop=True,
        sleep_fn=rewrite,
        max_checks=3,
    )
    texts = [payload["text"] for _url, payload in transport.sent]
    assert code == watchdog.EXIT_ARMED
    assert len(texts) == 1, "an old desk ticking along is one state, told once"
    assert "RESTARTED" not in texts[0]
    assert "publishes no run_id" in texts[0]
    assert "EVERY restart" in texts[0]


def test_the_identity_survives_every_state(tmp_path: Path) -> None:
    """A STALE or a disarmed desk still names its process.

    Otherwise a restart OUT of a bad state would be the one nobody hears
    about, which is the restart that matters most.
    """
    cfg = _cfg(tmp_path, mode="mt4")
    cfg.live_accepted = False
    engine = _engine(cfg, tmp_path, real_money=True)
    engine.step_all()
    engine.stop()
    path = watchdog.heartbeat_path_for(cfg.journal_path)
    published = _published_run_id(cfg)
    now = datetime.now(timezone.utc)
    disarmed = watchdog.decide(path, now=now, cfg=cfg)
    assert disarmed.state == watchdog.STATE_NOT_TRADING
    assert disarmed.run_id == published
    stale = watchdog.decide(path, now=now + timedelta(seconds=MT4_STALE + 10), cfg=cfg)
    assert stale.state == watchdog.STATE_STALE
    assert stale.run_id == published


# --- the started_at parse, which the crash-loop claim turns on -------------------------


@pytest.mark.parametrize(
    "text,expected_tz",
    [
        ("2026-10-08T12:00:00+00:00", timezone.utc),
        ("2026-10-08T12:00:00", timezone.utc),
        ("", None),
        ("not a timestamp", None),
    ],
)
def test_started_at_is_parsed_once_and_naive_reads_as_utc(
    text: str, expected_tz: timezone | None
) -> None:
    """One parse, used by the loop AND by the message it composes.

    Two copies would be two places for a naive stamp to be read differently,
    and the comparison behind the crash-loop claim is where that would bite: a
    stamp read as local on one side and UTC on the other invents or erases
    hours of apparent uptime.
    """
    parsed = watchdog._started_at_utc(text)
    if expected_tz is None:
        assert parsed is None
    else:
        assert parsed is not None and parsed.tzinfo == expected_tz


def test_an_unparseable_started_at_still_reports_the_restart() -> None:
    """The restart is the fact; the loop claim is the embellishment.

    A desk whose `started_at` is junk has still restarted, and losing the
    announcement because one field would not parse would be the silent case
    coming back through a side door.
    """
    report = watchdog.Report(
        state=watchdog.STATE_ARMED,
        reason="",
        exit_code=watchdog.EXIT_ARMED,
        text="straightedge ALIVE ARMED",
        next_sleep_s=10.0,
        stale_after_s=float(MT4_STALE),
        run_id="bbbbbbbb",
        started_at="not a timestamp",
    )
    text = watchdog.restart_text(
        previous_run_id="aaaaaaaa",
        previous_started_at=datetime.now(timezone.utc),
        report=report,
        restarts=2,
        watching_since=datetime.now(timezone.utc),
    )
    assert "RESTARTED" in text
    assert "restart 2" in text
    assert "CRASH LOOP" not in text


def test_a_first_run_with_no_previous_stamp_makes_no_loop_claim() -> None:
    """`previous_started_at` is None when the earlier heartbeat predated the field."""
    report = watchdog.Report(
        state=watchdog.STATE_ARMED,
        reason="",
        exit_code=watchdog.EXIT_ARMED,
        text="straightedge ALIVE ARMED",
        next_sleep_s=10.0,
        stale_after_s=float(MT4_STALE),
        run_id="bbbbbbbb",
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    text = watchdog.restart_text(
        previous_run_id="",
        previous_started_at=None,
        report=report,
        restarts=1,
        watching_since=datetime.now(timezone.utc),
    )
    assert "RESTARTED" in text
    assert "CRASH LOOP" not in text
    assert "previous run_id unknown" in text


def test_a_clock_that_moved_backwards_makes_no_loop_claim() -> None:
    """A negative lifetime says nothing about how long the previous run lived.

    Asserting a crash loop from it would be reading a clock fault as a
    diagnosis. The wrong clock is already alarmed by `_age_seconds`, which
    takes the worse of the desk's stamp and the filesystem.
    """
    now = datetime.now(timezone.utc)
    report = watchdog.Report(
        state=watchdog.STATE_ARMED,
        reason="",
        exit_code=watchdog.EXIT_ARMED,
        text="straightedge ALIVE ARMED",
        next_sleep_s=10.0,
        stale_after_s=float(MT4_STALE),
        run_id="bbbbbbbb",
        started_at=(now - timedelta(hours=3)).isoformat(),
    )
    text = watchdog.restart_text(
        previous_run_id="aaaaaaaa",
        previous_started_at=now,
        report=report,
        restarts=1,
        watching_since=now,
    )
    assert "RESTARTED" in text
    assert "CRASH LOOP" not in text
