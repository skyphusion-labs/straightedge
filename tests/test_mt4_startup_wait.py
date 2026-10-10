"""The cold-boot startup wait, and the steady-state budget it must not touch.

Measured on the live Windows host: after the first reboot since setup, MT4 came
back healthy and the desk was dead. `Engine.start()` called `connect()`, which
sent exactly ONE ping on the 5 second steady-state timeout, and MT4 is a GUI
application that needs tens of seconds to launch, load, log in and reach the
Expert's first timer tick. The ping timed out, the process exited, and nothing
retried, so every reboot silently killed the desk while every visible indicator
read healthy.

Everything here runs against a REAL mailbox directory, a REAL `FileBridge`, and
REAL wall-clock time. No clock is stubbed and no sleep is patched, because a
stubbed clock would prove the retry loop's arithmetic and nothing at all about
whether the adapter survives a mailbox that is genuinely empty for a while.
What makes that affordable is the thing the change added: the budget is a
parameter, so a test can ask for 1.5 seconds where production asks for 180.

Three things are asserted, and the second is the one that matters most:

1. A late Expert is WAITED for, and the desk connects.
2. An Expert that NEVER appears still fails, loudly and bounded, naming the
   elapsed time and what to check. A retry that cannot give up is the same
   defect wearing a fix's clothes, so the give-up path is driven on purpose.
3. The steady-state path (`connect`, `ensure_connected`) did NOT inherit the
   long budget. `Engine.step_all()` calls `ensure_connected()` on every step
   and `Engine._reconnect_broker()` calls `connect()` on the trading path, so a
   leaked cold-boot budget would turn a transient blip into a multi-minute
   stall while the desk holds live positions.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from mt4_transcripts import GOLDEN, Transcript, TranscriptExpert, ea_fail

from straightedge.broker import broker_for
from straightedge.broker.mt4_live import (
    DEFAULT_STARTUP_WAIT_SEC,
    BridgeTimeout,
    FileBridge,
    Mt4Broker,
    _log_line,
)
from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig, Mt4Config, SessionConfig, load_config
from straightedge.engine import Engine
from straightedge.synthetic import generate_bars

#: Short enough to keep the suite fast, long enough that a mailbox poll cycle
#: (20 ms) fits several times over even on a loaded CI runner.
BRIDGE_TIMEOUT = 0.4


class LateExpert:
    """A stand-in Expert that starts answering only after `delay` seconds.

    This is the whole point of the green proof. Before the delay there is no
    `.res` file and no reader of `.req` at all, which is byte for byte the state
    of the mailbox while MT4 is still launching.
    """

    def __init__(self, directory: Path, delay: float, transcripts: Any = GOLDEN) -> None:
        self.directory = directory
        self.delay = delay
        self.transcripts = transcripts
        self.ea: TranscriptExpert | None = None
        self._timer = threading.Timer(delay, self._start)

    def _start(self) -> None:
        ea = TranscriptExpert(self.directory, self.transcripts)
        ea.__enter__()
        self.ea = ea

    def __enter__(self) -> LateExpert:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._timer.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._timer.cancel()
        if self.ea is not None:
            self.ea.__exit__(exc_type, exc, tb)


def _broker(tmp_path: Path, *, wait: float, log: list[str] | None = None) -> Mt4Broker:
    lines = log if log is not None else []
    return Mt4Broker(
        FileBridge(tmp_path, timeout_sec=BRIDGE_TIMEOUT).call,
        magic=770077,
        startup_wait_sec=wait,
        log=lines.append,
    )


# ---------------------------------------------------------------------------
# 1. The green proof: a bridge that appears late is waited for.
# ---------------------------------------------------------------------------


@pytest.mark.timing
def test_startup_connect_waits_for_an_expert_that_appears_late(tmp_path: Path) -> None:
    """The cold boot, reproduced: the mailbox answers only after 1.2s.

    `BRIDGE_TIMEOUT` is 0.4s, so the first ping CANNOT succeed. Under the old
    one-shot `connect()` this is the exact sequence that killed the desk.
    """
    log: list[str] = []
    br = _broker(tmp_path, wait=8.0, log=log)
    with LateExpert(tmp_path, delay=1.2) as late:
        started = time.monotonic()
        br.startup_connect()
        elapsed = time.monotonic() - started
        assert late.ea is not None
        assert "ping" in late.ea.ops
    # It cannot have connected before the Expert existed.
    assert elapsed >= 1.2
    # And it did not simply burn the whole budget.
    assert elapsed < 8.0
    joined = "\n".join(log)
    assert "waiting up to 8s" in joined
    assert "no reply yet, attempt 1" in joined
    assert "Expert answered on attempt" in joined


def test_the_wait_says_it_is_waiting_with_an_elapsed_time(tmp_path: Path) -> None:
    """Silence for three minutes is indistinguishable from a hang.

    Asserted on the shape of the progress line rather than only on its
    existence: an attempt number and an elapsed figure are what let an operator
    reading `desk.out` tell a wait in progress from a wedged process.
    """
    log: list[str] = []
    br = _broker(tmp_path, wait=1.2, log=log)
    with pytest.raises(RuntimeError):
        br.startup_connect()
    progress = [line for line in log if "no reply yet" in line]
    assert progress, log
    assert "attempt 1 at " in progress[0]
    assert "of 1s" in progress[0]
    assert "retrying in " in progress[0]


def test_the_default_log_sink_writes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """The deployed desk redirects stdout to its log file, so that is the sink.

    A broker constructed without `log=` must reach stdout, not vanish. The
    flush is not observable from here; it is asserted by reading the source,
    and it matters because a redirected stdout is block-buffered and a
    three-minute wait would otherwise arrive as one burst after the fact.
    """
    _log_line("mt4: hello")
    assert "mt4: hello" in capsys.readouterr().out
    assert Mt4Broker(lambda op, p: {"ok": 1})._log is _log_line


# ---------------------------------------------------------------------------
# 2. The red proof: a bridge that never appears still fails, bounded.
# ---------------------------------------------------------------------------


@pytest.mark.timing
def test_startup_connect_gives_up_bounded_when_the_expert_never_appears(
    tmp_path: Path,
) -> None:
    """No Expert at all, ever. The wait MUST end and MUST say why.

    An unbounded retry would convert a wrong `files_dir`, a detached Expert or
    an MT4 that is not installed into a process that hangs forever looking
    busy, which is a worse failure than the crash it replaced, because the
    crash at least ended.
    """
    log: list[str] = []
    br = _broker(tmp_path, wait=1.5, log=log)
    started = time.monotonic()
    with pytest.raises(RuntimeError) as caught:
        br.startup_connect()
    elapsed = time.monotonic() - started
    msg = str(caught.value)

    # It gave up, and not before the budget was spent.
    assert elapsed >= 1.5
    # Bounded: the documented ceiling is the budget plus one bridge timeout.
    # The margin is CI slack, not a second policy; an unbounded loop never
    # reaches this line at all.
    assert elapsed < 1.5 + BRIDGE_TIMEOUT + 4.0

    # It retried rather than dying on the first timeout, which is the defect.
    assert len([line for line in log if "no reply yet" in line]) >= 1
    assert "ping(s) over" in msg

    # The give-up names the elapsed time, the budget and the diagnosis.
    assert "budget 2s" in msg or "budget 1s" in msg
    assert "mt4 bridge timeout" in msg
    assert "mt4.files_dir" in msg
    assert "Mt4RiskBot.mq4" in msg
    assert "AutoTrading" in msg

    # And the transport timeout is preserved as the cause, not swallowed.
    assert isinstance(caught.value.__cause__, BridgeTimeout)


def test_startup_connect_makes_more_than_one_attempt(tmp_path: Path) -> None:
    """The retry is the fix, so the attempt COUNT is asserted, not just the raise.

    A `startup_connect` that timed out once and re-raised would satisfy the
    give-up test above while changing nothing about the bug.
    """
    log: list[str] = []
    br = _broker(tmp_path, wait=1.5, log=log)
    with pytest.raises(RuntimeError) as caught:
        br.startup_connect()
    attempts = int(str(caught.value).split(" ping(s)")[0].rsplit(" ", 1)[1])
    assert attempts >= 2, log


# ---------------------------------------------------------------------------
# 3. The steady-state proof: the long budget did not leak.
# ---------------------------------------------------------------------------


@pytest.mark.timing
def test_connect_does_not_inherit_the_startup_budget(tmp_path: Path) -> None:
    """`Engine._reconnect_broker()` lands here, on the trading path.

    A 30 second budget leaking into `connect()` would stall the desk for 30
    seconds on a transient blip while it holds live positions.
    """
    br = _broker(tmp_path, wait=30.0)
    started = time.monotonic()
    with pytest.raises(BridgeTimeout, match="timeout"):
        br.connect()
    assert time.monotonic() - started < 5.0


@pytest.mark.timing
def test_ensure_connected_does_not_inherit_the_startup_budget(tmp_path: Path) -> None:
    """`Engine.step_all()` calls this on EVERY step."""
    br = _broker(tmp_path, wait=30.0)
    started = time.monotonic()
    with pytest.raises(BridgeTimeout, match="timeout"):
        br.ensure_connected()
    assert time.monotonic() - started < 5.0


def test_a_bridge_timeout_is_still_a_runtime_error() -> None:
    """Every existing caller catches `RuntimeError`; none of them was changed."""
    assert issubclass(BridgeTimeout, RuntimeError)


# ---------------------------------------------------------------------------
# What the wait deliberately does NOT retry.
# ---------------------------------------------------------------------------


@pytest.mark.timing
def test_startup_connect_does_not_retry_an_expert_that_refuses(tmp_path: Path) -> None:
    """An `ok=0` reply is a LIVE Expert stating a diagnosis. Waiting cannot fix it.

    This is why the retry is keyed on a TYPE (`BridgeTimeout`) and not on a
    message: "nothing is answering yet" and "the Expert answered no" are
    different facts, and burying the second under three minutes of retries
    would hide the operator's own error.
    """
    transcripts = {"ping": Transcript("ping", ea_fail(1, "no_terminal"))}
    br = Mt4Broker(
        FileBridge(tmp_path, timeout_sec=3.0).call,
        startup_wait_sec=30.0,
        log=lambda _msg: None,
    )
    with TranscriptExpert(tmp_path, transcripts) as ea:  # type: ignore[arg-type]
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="no_terminal") as caught:
            br.startup_connect()
        elapsed = time.monotonic() - started
    assert not isinstance(caught.value, BridgeTimeout)
    assert elapsed < 10.0
    assert ea.ops == ["ping"], "a refusal must not be retried"


@pytest.mark.timing
def test_a_zero_budget_is_one_ping(tmp_path: Path) -> None:
    """The pre-1.4.2 behaviour, kept reachable and meaning exactly itself.

    Zero is a real operator choice on a host where MT4 is already up before the
    desk starts, which is why an UNSET key stays `None` in `Mt4Config` rather
    than collapsing into the same value as a configured zero.
    """
    log: list[str] = []
    br = _broker(tmp_path, wait=0.0, log=log)
    started = time.monotonic()
    with pytest.raises(BridgeTimeout):
        br.startup_connect()
    assert time.monotonic() - started < 5.0
    assert log == []


def test_a_zero_budget_connects_immediately_when_the_expert_is_there(
    tmp_path: Path,
) -> None:
    """The no-wait path has to WORK, not merely fail fast.

    A zero budget that only ever raised would leave the pre-1.4.2 behaviour
    half tested: the operator who sets `startup_wait_sec = 0` on a host where
    MT4 is already up must get a connect, silently, with no progress lines.
    """
    log: list[str] = []
    br = _broker(tmp_path, wait=0.0, log=log)
    with TranscriptExpert(tmp_path, GOLDEN) as ea:  # type: ignore[arg-type]
        br.startup_connect()
    assert ea.ops == ["ping"]
    assert log == []


def test_a_negative_budget_is_clamped_not_inverted(tmp_path: Path) -> None:
    br = _broker(tmp_path, wait=-10.0)
    assert br._startup_wait == 0.0


# ---------------------------------------------------------------------------
# The wiring. A budget nothing calls is not a fix.
# ---------------------------------------------------------------------------


class _RecordingBroker:
    """A broker that offers both doors and records which one the engine used."""

    def __init__(self, inner: PaperBroker) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def connect(self) -> None:
        self.calls.append("connect")
        self._inner.connect()

    def startup_connect(self) -> None:
        self.calls.append("startup_connect")
        self._inner.connect()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _OldBroker:
    """A venue with no startup door. Paper and MT5 are both this shape."""

    def __init__(self, inner: PaperBroker) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def connect(self) -> None:
        self.calls.append("connect")
        self._inner.connect()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _engine(tmp_path: Path, broker: Any) -> Engine:
    cfg = BotConfig()
    cfg.session = SessionConfig(enabled=False)
    cfg.symbols = ["EURUSD"]
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    return Engine(cfg, broker, halt_dir=str(tmp_path))


def _paper() -> PaperBroker:
    inner = PaperBroker(balance=10_000)
    inner.seed_bars("EURUSD", generate_bars(80, drift=0.0004, seed=3))
    return inner


def test_engine_start_uses_the_startup_door_when_the_venue_offers_one(
    tmp_path: Path,
) -> None:
    broker = _RecordingBroker(_paper())
    engine = _engine(tmp_path, broker)
    engine.start()
    engine.stop()
    assert broker.calls == ["startup_connect"], broker.calls


def test_engine_start_falls_back_to_connect_for_a_venue_without_one(
    tmp_path: Path,
) -> None:
    broker = _OldBroker(_paper())
    engine = _engine(tmp_path, broker)
    engine.start()
    engine.stop()
    assert broker.calls == ["connect"], broker.calls


def test_broker_for_passes_the_configured_budget() -> None:
    cfg = BotConfig()
    cfg.mode = "mt4"
    cfg.mt4 = Mt4Config(files_dir="/tmp/straightedge-mt4-none", startup_wait_sec=42.0)
    assert broker_for(cfg)._startup_wait == 42.0


def test_broker_for_uses_the_adapter_default_when_the_key_is_unset() -> None:
    """One declaration of the default, in the module that implements the wait."""
    cfg = BotConfig()
    cfg.mode = "mt4"
    cfg.mt4 = Mt4Config(files_dir="/tmp/straightedge-mt4-none")
    assert cfg.mt4.startup_wait_sec is None
    assert broker_for(cfg)._startup_wait == DEFAULT_STARTUP_WAIT_SEC


def test_config_reads_startup_wait_sec(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[account]\nmode = "mt4"\n[mt4]\nstartup_wait_sec = 90\n', encoding="utf-8"
    )
    assert load_config(path).mt4.startup_wait_sec == 90.0


def test_config_leaves_startup_wait_sec_unset_when_absent(tmp_path: Path) -> None:
    """Absent is not zero, and it is not 180 either. It is unset."""
    path = tmp_path / "config.toml"
    path.write_text('[account]\nmode = "mt4"\n[mt4]\ntimeout_ms = 5000\n', encoding="utf-8")
    assert load_config(path).mt4.startup_wait_sec is None


def test_config_reads_a_configured_zero_as_zero(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[account]\nmode = "mt4"\n[mt4]\nstartup_wait_sec = 0\n', encoding="utf-8")
    assert load_config(path).mt4.startup_wait_sec == 0.0


def test_the_shipped_example_configs_declare_the_budget() -> None:
    """The knob is documented where the operator looks for it."""
    root = Path(__file__).resolve().parents[1]
    for name in ("config.example.toml", "config.handover.toml"):
        text = (root / name).read_text(encoding="utf-8")
        assert "startup_wait_sec" in text, name
