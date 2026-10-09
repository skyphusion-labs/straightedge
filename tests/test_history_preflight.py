"""Issue #33: a configured symbol with no price series was silently untradeable.

What was measured on the live rig, through the real MT4 mailbox, with H4 charts
open and the desk configured for H1:

    EURUSD  bars=0    ATR=nan
    USDJPY  bars=0    ATR=nan
    XAUUSD  bars=200  ATR=17.2188

XAUUSD was the one symbol with an H1 chart open. Nothing crashed and nothing
said anything: `Engine.step_symbol` asks for bars, gets an empty list, and
returns with no journal record, so the operator-visible symptom was "the bot
will not trade EURUSD" with no cause anywhere. The spread gate is NOT what
refused, which is worth stating because it is the obvious guess and it is wrong:
`risk.py:523` reads `signal.atr > 0`, which is False for nan, so
`spread_too_wide` cannot fire. There was no refusal at all. There was a silent
return.

Three kinds of evidence here, and they are not interchangeable.

1. Unit, against a fixture venue. The load-bearing one is
   `TestDistinguishesSymbols`: a venue serving 0 bars for one symbol and 200 for
   another must produce two DIFFERENT verdicts. A check that passed on both, or
   failed on both, would be green against exactly the state that was measured,
   so "it passes" is worth nothing unless the pass can also separate them.
2. Wire, through a real file mailbox and the Expert's own reply text. This is
   where `bars_total` / `selected` / `history_error` are shown to arrive, and
   where an Expert too old to emit them is shown to read as COULD NOT MEASURE
   rather than as zeros.
3. Source guards over the shipped `.mq4`. MQL4 does not execute in this suite,
   so these assert the structural invariants of `RatesReply` (it selects the
   symbol, it touches the series even when it has no bars to return, it reports
   the state, and it does not Sleep in the mailbox). Mutate the Expert and they
   go red; they are not a behavioural test of a running terminal, and no test in
   this repo can be.

Live verification is NOT claimed. The rig is reachable from the lead's laptop
only, so nothing here observes a real history download. What these tests do
guarantee is that the desk states the condition instead of hiding it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from straightedge.models import VenueClock
from straightedge.broker.mt4_live import FileBridge, Mt4Broker
from straightedge.config import BotConfig
from straightedge.engine import Engine, _format_event
from straightedge.history import (
    ATTEMPTS,
    MT4_HISTORY_UPDATING,
    MT4_NO_HISTORY,
    STATUS_ERROR,
    STATUS_NONE,
    STATUS_PARTIAL,
    STATUS_WARMING,
    STATUSES,
    HistoryProbe,
    preflight,
    waiting_helps,
)
from straightedge.synthetic import generate_bars
from mt4_transcripts import (
    GOLDEN,
    TranscriptExpert,
    bar_rows,
    ea_kv,
    t_rates,
    t_rates_cold,
)

EA_PATH = Path(__file__).resolve().parents[1] / "mt4" / "Experts" / "Mt4RiskBot.mq4"

NEEDED = 30
ATR_PERIOD = 14
TIMEOUT = 5.0


def warm(n: int = 200, seed: int = 3) -> list:
    """A series long enough for the strategy, with a real range so ATR is finite."""
    return generate_bars(n, drift=0.0003, vol=0.0002, seed=seed)


class _Terminal:
    """A venue that serves exactly the series it was given, per symbol.

    `history_async` is set because MT4 and MT5 fetch history in the background,
    which is the only condition under which asking again can change the answer.
    """

    history_async = True

    def __init__(self, series: dict[str, list]) -> None:
        self._series = {k.upper(): list(v) for k, v in series.items()}
        self.asked: list[tuple[str, int]] = []

    def rates(self, name: str, timeframe: str, count: int) -> list:
        self.asked.append((name.upper(), int(count)))
        return self._series.get(name.upper(), [])[-int(count) :]


class _ReportingTerminal(_Terminal):
    """A venue that also reports the state of the series, the way MT4 now does."""

    def __init__(self, series: dict[str, list], state: dict[str, dict]) -> None:
        super().__init__(series)
        self._state = {k.upper(): v for k, v in state.items()}

    def history_probe(self, name: str, timeframe: str, count: int) -> HistoryProbe:
        bars = self.rates(name, timeframe, count)
        extra = self._state.get(name.upper(), {})
        return HistoryProbe(
            bars=bars,
            bars_total=extra.get("bars_total", len(bars)),
            selected=extra.get("selected", True),
            history_error=extra.get("history_error", 0),
        )


class _GrowingTerminal(_Terminal):
    """Serves nothing until the Nth ask, the way a download completing looks."""

    def __init__(self, symbol: str, ready_on: int, bars: list) -> None:
        super().__init__({})
        self._symbol = symbol.upper()
        self._ready_on = ready_on
        self._bars = bars
        self.calls = 0

    def rates(self, name: str, timeframe: str, count: int) -> list:
        self.calls += 1
        self.asked.append((name.upper(), int(count)))
        if name.upper() == self._symbol and self.calls >= self._ready_on:
            return self._bars[-int(count) :]
        return []


class _AngryTerminal(_Terminal):
    def rates(self, name: str, timeframe: str, count: int) -> list:
        raise RuntimeError("mt4 bridge timeout")


def run(broker: object, symbols: list[str], **kw):
    kw.setdefault("timeframe", "H1")
    kw.setdefault("needed", NEEDED)
    kw.setdefault("atr_period", ATR_PERIOD)
    kw.setdefault("attempts", 1)
    kw.setdefault("sleep", lambda _s: None)
    return preflight(broker, symbols, **kw)


# ---------------------------------------------------------------------------
# 1. The load-bearing requirement.
# ---------------------------------------------------------------------------


class TestDistinguishesSymbols:
    """0 bars for one symbol and 200 for another must be two verdicts, not one."""

    def report(self):
        return run(
            _Terminal({"XAUUSD": warm(200)}),
            ["EURUSD", "XAUUSD"],
        )

    def test_exactly_one_of_two_is_unusable(self) -> None:
        """The denominator is printed: 1 of 2, not "something is wrong"."""
        got = self.report()
        assert len(got.symbols) == 2
        assert len(got.unusable) == 1
        assert got.names() == ["EURUSD"]
        assert got.ok is False

    def test_the_warm_symbol_passes_and_the_cold_one_does_not(self) -> None:
        got = self.report()
        cold, hot = got.symbols
        assert cold.symbol == "EURUSD"
        assert hot.symbol == "XAUUSD"
        assert cold.usable is False
        assert hot.usable is True
        assert cold.bars == 0
        assert hot.bars >= NEEDED

    def test_a_check_that_passed_on_both_would_be_red_here(self) -> None:
        """The negative control for this whole module.

        Two ways to be uselessly green on the measured state: treat "some symbol
        has history" as a pass, or treat "any symbol lacks history" as a total
        failure. The first reports XAUUSD's 200 bars as the desk's health; the
        second loses which symbol is dead. Both are asserted against here.
        """
        got = self.report()
        assert [s.usable for s in got.symbols] == [False, True]
        text = got.text()
        assert "EURUSD" in text
        assert "XAUUSD" not in text

    def test_no_atr_is_borrowed_from_the_symbol_that_has_one(self) -> None:
        """The rule `broker/mt4_live.py:294-308` exists to enforce.

        A missing ATR is None. It is not 0.0, not nan, and above all not the
        other symbol's 17.2188: `risk.py` recomputes from the same numbers, so a
        borrowed value would be compared against itself and pass.
        """
        cold, hot = self.report().symbols
        assert cold.atr is None
        assert hot.atr is not None
        assert hot.atr > 0
        assert cold.atr != hot.atr

    def test_each_symbol_is_named_in_its_own_line(self) -> None:
        """`bars` is what the venue SERVED, and the ask is `needed + 2`.

        Not the whole file: `Engine.step_symbol` asks for `needed + 2` too, so
        the preflight measures the same window the desk will actually read. How
        much the terminal HOLDS is a separate measurement and has its own field,
        `bars_total`, which only a venue that reports it can fill in.
        """
        lines = self.report().lines()
        assert lines[0].startswith("EURUSD: 0 bars (need 30), ATR unavailable")
        assert lines[1].startswith(f"XAUUSD: {NEEDED + 2} bars (need 30), ATR=")

    def test_bars_served_and_bars_held_are_separate_measurements(self) -> None:
        got = run(
            _ReportingTerminal({"XAUUSD": warm(200)}, {"XAUUSD": {"bars_total": 200}}),
            ["XAUUSD"],
        ).symbols[0]
        assert got.bars == NEEDED + 2
        assert got.bars_total == 200
        assert got.usable is True


# ---------------------------------------------------------------------------
# 2. The roster: every state this can be in, and they read differently.
# ---------------------------------------------------------------------------


class TestStatusRoster:
    def test_every_status_in_the_roster_is_reachable(self) -> None:
        """Print the denominator: five statuses declared, five produced.

        A roster stops being a denominator the moment a value can be produced
        that is not in it, or a value in it cannot be produced (#61).
        """
        produced = {
            run(_Terminal({"A": warm(200)}), ["A"]).symbols[0].status,
            run(_Terminal({"A": warm(20)}), ["A"]).symbols[0].status,
            run(
                _ReportingTerminal({}, {"A": {"history_error": MT4_HISTORY_UPDATING}}),
                ["A"],
            )
            .symbols[0]
            .status,
            run(_Terminal({}), ["A"]).symbols[0].status,
            run(_AngryTerminal({}), ["A"]).symbols[0].status,
        }
        assert produced == set(STATUSES)
        assert len(STATUSES) == 5

    def test_still_downloading_and_never_coming_say_different_things(self) -> None:
        """The distinction the wire change exists to make possible.

        Before the Expert reported `history_error`, both of these were `n=0` and
        the desk had to guess. One is "wait longer", the other is "the symbol is
        wrong". A single message for both is the reassuring reading of two.
        """
        warming = run(
            _ReportingTerminal({}, {"A": {"history_error": MT4_HISTORY_UPDATING}}), ["A"]
        ).symbols[0]
        absent = run(
            _ReportingTerminal({}, {"A": {"history_error": MT4_NO_HISTORY}}), ["A"]
        ).symbols[0]
        assert warming.status == STATUS_WARMING
        assert absent.status == STATUS_NONE
        assert "STILL DOWNLOADING" in warming.line()
        assert "NO HISTORY DATA" in absent.line()
        assert warming.line() != absent.line()

    def test_partial_history_is_a_permanent_non_trade_and_says_so(self) -> None:
        """20 bars is not 0 bars and is not enough.

        `Strategy.signal` returns FLAT `warmup` below `needed_bars()`
        (`strategy.py:25-27`), so a half-downloaded series produces an ATR, looks
        alive, and never trades. That is the state most likely to be read as
        healthy, so it is named explicitly.
        """
        got = run(_Terminal({"A": warm(20)}), ["A"]).symbols[0]
        assert got.status == STATUS_PARTIAL
        assert got.bars == 20
        assert got.atr is not None  # computable, and still useless
        assert got.usable is False
        assert "NOT ENOUGH" in got.line()
        assert "warmup forever" in got.line()

    def test_a_venue_that_will_not_answer_is_could_not_measure(self) -> None:
        got = run(_AngryTerminal({}), ["A"]).symbols[0]
        assert got.status == STATUS_ERROR
        assert got.usable is False
        assert "COULD NOT MEASURE" in got.line()
        assert "bridge timeout" in got.line()

    def test_an_unselectable_symbol_is_named_as_that(self) -> None:
        got = run(_ReportingTerminal({}, {"A": {"selected": False}}), ["A"]).symbols[0]
        assert got.selected is False
        assert "could not select" in got.line()


# ---------------------------------------------------------------------------
# 3. The bounded wait, which is the warm-up itself.
# ---------------------------------------------------------------------------


class TestBoundedWait:
    def test_asking_is_the_warm_up_and_a_late_series_is_accepted(self) -> None:
        """The whole point of path (B): the ask triggers the fetch, the desk waits.

        The terminal serves nothing for two asks and then 200 bars. The report
        must come back usable, and must record that it took three attempts, so a
        slow link reads as a slow link and not as a dead symbol.
        """
        slept: list[float] = []
        term = _GrowingTerminal("EURUSD", ready_on=3, bars=warm(200))
        got = preflight(
            term,
            ["EURUSD"],
            timeframe="H1",
            needed=NEEDED,
            atr_period=ATR_PERIOD,
            attempts=6,
            delay=0.25,
            sleep=slept.append,
        )
        assert got.ok is True
        assert got.symbols[0].attempts == 3
        assert term.calls == 3
        assert slept == [0.25, 0.25]

    def test_the_happy_path_never_sleeps(self) -> None:
        slept: list[float] = []
        got = preflight(
            _Terminal({"A": warm(200)}),
            ["A"],
            timeframe="H1",
            needed=NEEDED,
            atr_period=ATR_PERIOD,
            attempts=ATTEMPTS,
            sleep=slept.append,
        )
        assert got.ok is True
        assert slept == []

    def test_only_the_missing_symbol_is_asked_again(self) -> None:
        slept: list[float] = []
        term = _Terminal({"XAUUSD": warm(200)})
        preflight(
            term,
            ["EURUSD", "XAUUSD"],
            timeframe="H1",
            needed=NEEDED,
            atr_period=ATR_PERIOD,
            attempts=3,
            sleep=slept.append,
        )
        assert [name for name, _count in term.asked].count("XAUUSD") == 1
        assert [name for name, _count in term.asked].count("EURUSD") == 3
        assert len(slept) == 2

    def test_the_wait_is_bounded_and_the_budget_is_reported(self) -> None:
        slept: list[float] = []
        got = preflight(
            _Terminal({}),
            ["A"],
            timeframe="H1",
            needed=NEEDED,
            atr_period=ATR_PERIOD,
            attempts=4,
            delay=0.5,
            sleep=slept.append,
        )
        assert got.ok is False
        assert got.attempts == 4
        assert slept == [0.5, 0.5, 0.5]
        assert "after 4 attempts" in got.symbols[0].line()

    def test_a_venue_that_answers_from_memory_is_asked_once(self) -> None:
        """`run_backtest` seeds ONE bar per symbol on purpose and streams the rest.

        A ten-second wait there would be ten seconds added to every backtest in
        exchange for a verdict that was already correct at attempt one, so the
        wait is gated on the venue declaring that waiting could help.
        """
        assert waiting_helps(_Terminal({})) is True

        class _Memory:
            def __init__(self) -> None:
                self.calls = 0

            def rates(self, name, timeframe, count):
                self.calls += 1
                return []

        mem = _Memory()
        assert waiting_helps(mem) is False
        got = preflight(
            mem,
            ["A"],
            timeframe="H1",
            needed=NEEDED,
            atr_period=ATR_PERIOD,
            sleep=lambda _s: pytest.fail("a memory venue must not be waited on"),
        )
        assert mem.calls == 1
        assert got.attempts == 1
        assert got.ok is False

    def test_a_backtest_does_not_pay_the_wait(self) -> None:
        """The live proof of the line above, through the real backtest entry point."""
        from straightedge.engine import run_backtest

        cfg = BotConfig()
        cfg.symbols = ["EURUSD"]
        cfg.session.enabled = False
        series = {"EURUSD": warm(120)}
        result = run_backtest(cfg, series, journal_path="/dev/null")
        assert "equity" in result


# ---------------------------------------------------------------------------
# 4. Nothing is filled in.
# ---------------------------------------------------------------------------


class TestNoFabrication:
    def test_a_missing_series_is_reported_as_missing_everywhere(self) -> None:
        row = run(_Terminal({}), ["EURUSD"]).rows()[0]
        assert row["symbol"] == "EURUSD"
        assert row["bars"] == 0
        assert row["atr"] is None
        assert row["status"] == STATUS_NONE

    def test_a_venue_that_reports_nothing_reads_as_nothing_not_as_zero(self) -> None:
        """An Expert too old to emit the state fields must not look like one that
        emitted zeros. Same partition as `survivor_ticket`."""
        row = run(_Terminal({}), ["EURUSD"]).rows()[0]
        assert row["bars_total"] is None
        assert row["selected"] is None
        assert row["history_error"] is None
        assert "COULD NOT MEASURE whether the download is in flight" in (
            run(_Terminal({}), ["EURUSD"]).symbols[0].line()
        )

    def test_the_remedy_never_tells_the_operator_to_open_a_chart(self) -> None:
        """A rule whose compliance depends on a human remembering is not a control.

        The software fetches its own data; when it genuinely cannot, the message
        is about the symbol name and the broker, which the operator can act on
        once, not about a window they must keep open forever.
        """
        text = run(_Terminal({}), ["EURUSD"]).text()
        lowered = text.lower()
        assert "chart" not in lowered
        assert "symbol name" in lowered
        assert "EURUSD" in text

    def test_the_all_clear_is_empty_rather_than_reassuring(self) -> None:
        got = run(_Terminal({"A": warm(200)}), ["A"])
        assert got.ok is True
        assert got.text() == ""


# ---------------------------------------------------------------------------
# 5. Engine startup: the report reaches the journal and the chat.
# ---------------------------------------------------------------------------


def _engine(tmp_path: Path, symbols: list[str], series: dict[str, list]) -> Engine:
    from straightedge.broker.paper import PaperBroker

    cfg = BotConfig()
    cfg.symbols = list(symbols)
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.journal_path = str(tmp_path / "j.jsonl")
    broker = PaperBroker(balance=10000.0)
    for name, bars in series.items():
        broker.seed_bars(name, bars)
    return Engine(cfg, broker, halt_dir=str(tmp_path))


def _events(tmp_path: Path, name: str = "j.jsonl") -> list[dict]:
    import json

    path = tmp_path / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestEngineStartup:
    def test_start_journals_every_symbol_and_names_the_dead_one(self, tmp_path: Path) -> None:
        eng = _engine(tmp_path, ["EURUSD", "XAUUSD"], {"XAUUSD": warm(200)})
        eng.start()
        events = _events(tmp_path)
        pre = [e for e in events if e["event"] == "history_preflight"]
        loud = [e for e in events if e["event"] == "history_unavailable"]
        assert len(pre) == 1
        assert pre[0]["measured"] == 2
        assert [r["symbol"] for r in pre[0]["symbols"]] == ["EURUSD", "XAUUSD"]
        assert pre[0]["unusable"] == ["EURUSD"]
        assert len(loud) == 1
        assert loud[0]["unusable"] == ["EURUSD"]
        assert "EURUSD" in loud[0]["text"]
        assert eng.history is not None
        assert eng.history.ok is False

    def test_a_healthy_start_records_the_denominator_and_stays_quiet(
        self, tmp_path: Path
    ) -> None:
        """The all-clear is journaled and NOT announced.

        The row for a healthy symbol is what makes a later regression visible,
        so it is recorded; a routine all-clear in the chat is noise that trains
        the operator to ignore the channel the real failure arrives on.
        """
        eng = _engine(tmp_path, ["EURUSD"], {"EURUSD": warm(200)})
        eng.start()
        events = _events(tmp_path)
        assert [e["event"] for e in events].count("history_preflight") == 1
        assert [e["event"] for e in events].count("history_unavailable") == 0
        assert eng.history is not None and eng.history.ok is True

    def test_history_before_start_is_none_not_healthy(self, tmp_path: Path) -> None:
        eng = _engine(tmp_path, ["EURUSD"], {"EURUSD": warm(200)})
        assert eng.history is None

    def test_the_loud_event_is_the_one_that_reaches_the_chat(self) -> None:
        assert _format_event("history_preflight", {"unusable": ["EURUSD"]}) == ""
        assert _format_event("history_unavailable", {"text": "NO USABLE H1 HISTORY: EURUSD"}) == (
            "NO USABLE H1 HISTORY: EURUSD"
        )

    def test_add_symbol_reports_a_cold_symbol_instead_of_only_added(
        self, tmp_path: Path
    ) -> None:
        """A symbol added mid-run misses the startup warm entirely.

        `added USDJPY` on its own would report a symbol that cannot trade as a
        success, which is the same silence at a different door.
        """
        eng = _engine(tmp_path, ["EURUSD"], {"EURUSD": warm(200)})
        eng.start()
        reply = eng.add_symbol("usdjpy")
        assert "USDJPY" in reply
        assert "NO USABLE H1 HISTORY" in reply
        assert "added USDJPY anyway" in reply
        assert "USDJPY" in eng.cfg.symbols

    def test_add_symbol_with_history_just_says_added(self, tmp_path: Path) -> None:
        eng = _engine(tmp_path, ["EURUSD"], {"EURUSD": warm(200), "XAUUSD": warm(200, seed=9)})
        eng.start()
        reply = eng.add_symbol("xauusd")
        assert reply.startswith("added XAUUSD")
        assert "NO USABLE" not in reply


# ---------------------------------------------------------------------------
# 6. The gates: doctor and run.
# ---------------------------------------------------------------------------


class TestDoctorGate:
    def test_plain_doctor_says_it_did_not_measure(self, capsys, monkeypatch) -> None:
        """An absent check reads exactly like a passed one unless it says so."""
        from straightedge.__main__ import main

        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("ACCOUNT_MODE", raising=False)
        assert main(["doctor"]) == 0
        out = capsys.readouterr().out
        assert "history: NOT MEASURED" in out
        assert "needs --connect" in out

    def test_doctor_connect_goes_red_on_a_symbol_with_no_history(
        self, capsys, monkeypatch, tmp_path: Path
    ) -> None:
        """The required red. A config naming a symbol with no series must make
        `doctor` say so, by name, and exit non-zero.

        Non-zero on purpose: #33 B1 makes `doctor` exit 0 part of the pass
        condition before any run, and `[symbols] names` is the SCAN list that
        drives the loop (`config.py:247-253`), so a name in it is a symbol the
        desk will try to trade. Green here would be doctor clearing a run with a
        dead instrument in it.
        """
        from straightedge.__main__ import main

        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setenv("ACCOUNT_MODE", "mt4")
        monkeypatch.setenv("MT4_FILES_DIR", str(tmp_path))
        cfg_path = tmp_path / "c.toml"
        cfg_path.write_text(
            '[account]\nmode = "mt4"\n[symbols]\nnames = ["EURUSD", "XAUUSD"]\n',
            encoding="utf-8",
        )

        class ColdEurusd:
            history_async = True

            def __init__(
                self,
                call,
                *,
                magic: int = 0,
                startup_wait_sec: float = 0.0,
                send_timeout_sec: float = 0.0,
            ) -> None:
                del call, magic, startup_wait_sec, send_timeout_sec

            def connect(self) -> None:
                return None

            def disconnect(self) -> None:
                return None

            def account(self):
                return type(
                    "Acct",
                    (),
                    {
                        "login": 42,
                        "server": "MT4-Demo",
                        "equity": 10000.0,
                        "currency": "USD",
                        "trade_mode": 0,
                    },
                )()

            def history_probe(self, name, timeframe, count):
                if name.upper() == "XAUUSD":
                    return HistoryProbe(
                        bars=warm(200)[-int(count) :],
                        bars_total=200,
                        selected=True,
                        history_error=0,
                    )
                return HistoryProbe(bars=[], bars_total=0, selected=True, history_error=0)

            # A venue has to be able to state its clock (straightedge#172).
            # Zero is this fake stamping UTC, which keeps THIS test's
            # subject unchanged; the clock's own suite is
            # tests/test_venue_clock.py.
            def venue_clock(self, name, *, max_staleness_sec=None):
                del name, max_staleness_sec
                return VenueClock.declared(0, source="fake")

        monkeypatch.setattr("straightedge.broker.mt4_live.Mt4Broker", ColdEurusd)
        monkeypatch.setattr("straightedge.history.ATTEMPTS", 2)
        rc = main(["--config", str(cfg_path), "doctor", "--connect"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "1 of 2 usable" in out
        assert "EURUSD: 0 bars" in out
        assert "ATR unavailable" in out
        assert "NO USABLE H1 HISTORY: EURUSD" in out
        assert "XAUUSD: 62 bars" in out
        assert "ATR=" in out

    def test_doctor_connect_stays_green_when_every_symbol_has_history(
        self, capsys, monkeypatch, tmp_path: Path
    ) -> None:
        """The other half: the red above has to be the condition, not the check."""
        from straightedge.__main__ import main

        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setenv("ACCOUNT_MODE", "mt4")
        monkeypatch.setenv("MT4_FILES_DIR", str(tmp_path))
        cfg_path = tmp_path / "c.toml"
        cfg_path.write_text(
            '[account]\nmode = "mt4"\n[symbols]\nnames = ["EURUSD", "XAUUSD"]\n',
            encoding="utf-8",
        )

        class Warm:
            def __init__(
                self,
                call,
                *,
                magic: int = 0,
                startup_wait_sec: float = 0.0,
                send_timeout_sec: float = 0.0,
            ) -> None:
                del call, magic, startup_wait_sec, send_timeout_sec

            def connect(self) -> None:
                return None

            def disconnect(self) -> None:
                return None

            def account(self):
                return type(
                    "Acct",
                    (),
                    {
                        "login": 42,
                        "server": "MT4-Demo",
                        "equity": 10000.0,
                        "currency": "USD",
                        "trade_mode": 0,
                    },
                )()

            def rates(self, name, timeframe, count):
                return warm(200)

            # A venue has to be able to state its clock (straightedge#172).
            # Zero is this fake stamping UTC, which keeps THIS test's
            # subject unchanged; the clock's own suite is
            # tests/test_venue_clock.py.
            def venue_clock(self, name, *, max_staleness_sec=None):
                del name, max_staleness_sec
                return VenueClock.declared(0, source="fake")

        monkeypatch.setattr("straightedge.broker.mt4_live.Mt4Broker", Warm)
        rc = main(["--config", str(cfg_path), "doctor", "--connect"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "2 of 2 usable" in out
        assert "NO USABLE" not in out


class TestRunGate:
    def test_run_refuses_to_start_a_desk_with_a_dead_symbol(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """Ordered preference: it-just-works first, a NAMED failure second.

        A silent non-trade is on neither list, so the run does not begin. The
        symbols are on stderr and the exit code is non-zero.
        """
        from straightedge.__main__ import main

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234567890:" + "A" * 35)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
        cfg = tmp_path / "c.toml"
        cfg.write_text(
            '[account]\nmode = "paper"\n[symbols]\nnames = ["EURUSD", "GBPUSD"]\n',
            encoding="utf-8",
        )
        rc = main(["--config", str(cfg), "run", "--mode", "paper"])
        err = capsys.readouterr().err
        assert rc == 2
        assert "NO USABLE H1 HISTORY" in err
        assert "EURUSD" in err
        assert "GBPUSD" in err
        assert "2 of 2 configured symbols" in err


# ---------------------------------------------------------------------------
# 7. The wire, through a real mailbox.
# ---------------------------------------------------------------------------


def _mt4(tmp_path: Path) -> Mt4Broker:
    return Mt4Broker(FileBridge(tmp_path, timeout_sec=TIMEOUT).call, magic=770077)


def _route(cold: str, hot: str, *, hot_rows: int = 200, history_error: int = 0, state: bool = True):
    """Route a `rates` request by the symbol it asks for.

    MT4 history is per symbol AND timeframe, so a fixture that varies only by op
    cannot express the state that was measured, and a test that cannot express it
    would pass against a desk that treated every symbol the same.
    """

    def choose(op: str, body: str):
        if op != "rates":
            return None
        symbol = ea_kv(body, "symbol").upper()
        if symbol == hot:
            return t_rates(bar_rows(hot_rows), state=state)
        if symbol == cold:
            return t_rates_cold(history_error=history_error) if state else t_rates([], state=False)
        return None

    return choose


class TestWire:
    def test_the_expert_reports_the_state_of_the_series(self, tmp_path: Path) -> None:
        with TranscriptExpert(tmp_path, dict(GOLDEN)) as ea:
            probe = _mt4(tmp_path).history_probe(
                "EURUSD", "H1", 2
            )
        assert ea_kv(ea.request("rates"), "timeframe") == "H1"
        assert len(probe.bars) == 2
        assert probe.bars_total == 2
        assert probe.selected is True
        assert probe.history_error == 0

    def test_a_cold_symbol_arrives_with_its_reason(self, tmp_path: Path) -> None:
        with TranscriptExpert(
            tmp_path, dict(GOLDEN, rates=t_rates_cold(history_error=MT4_HISTORY_UPDATING))
        ):
            probe = _mt4(tmp_path).history_probe("EURUSD", "H1", 200)
        assert probe.bars == []
        assert probe.bars_total == 0
        assert probe.history_error == MT4_HISTORY_UPDATING

    def test_an_expert_too_old_to_answer_reads_as_unanswered(self, tmp_path: Path) -> None:
        """The pre-#33 Expert emitted `n=` and the rows and nothing else.

        It is still attachable in a terminal, so its silence must read as silence.
        Reporting 0 there would turn "nobody asked" into "no problem", which is
        the defect `_survivor_ticket` was written to avoid in the trade path.
        """
        with TranscriptExpert(tmp_path, dict(GOLDEN, rates=t_rates([], state=False))):
            probe = _mt4(tmp_path).history_probe("EURUSD", "H1", 200)
        assert probe.bars == []
        assert probe.bars_total is None
        assert probe.selected is None
        assert probe.history_error is None

    def test_rates_still_returns_bars_and_only_bars(self, tmp_path: Path) -> None:
        """`rates()` is implemented in terms of `history_probe`, so the wire is
        parsed in one place; its own contract is unchanged."""
        with TranscriptExpert(tmp_path, dict(GOLDEN)):
            bars = _mt4(tmp_path).rates("eurusd", "H1", 2)
        assert [b.close for b in bars] == [1.10120, 1.10200]

    def test_the_measured_rig_state_over_the_real_wire(self, tmp_path: Path) -> None:
        """EURUSD 0 bars, XAUUSD 200, through a file mailbox, end to end.

        This is the closest this suite gets to the rig: the Expert's own reply
        text, the adapter's real decoder, and the preflight on top. It still does
        not observe a history download, which no test here can.
        """
        broker_calls: list[str] = []

        with TranscriptExpert(
            tmp_path, dict(GOLDEN), route=_route("EURUSD", "XAUUSD")
        ) as ea:
            got = preflight(
                _mt4(tmp_path),
                ["EURUSD", "XAUUSD"],
                timeframe="H1",
                needed=NEEDED,
                atr_period=ATR_PERIOD,
                attempts=2,
                delay=0.0,
                sleep=lambda _s: broker_calls.append("slept"),
            )
        assert ea.ops.count("rates") == 3  # both once, then EURUSD again
        assert got.names() == ["EURUSD"]
        assert got.symbols[0].bars == 0
        assert got.symbols[0].atr is None
        assert got.symbols[1].bars == 200
        assert got.symbols[1].atr is not None and got.symbols[1].atr > 0
        assert broker_calls == ["slept"]


# ---------------------------------------------------------------------------
# 8. Source guards over the shipped Expert. CI cannot compile MQL4.
# ---------------------------------------------------------------------------


def _function_body(src: str, signature: str) -> str:
    """The brace-matched body of one MQL4 function, signature included."""
    start = src.index(signature)
    open_brace = src.index("{", start)
    depth = 0
    for i in range(open_brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


class TestExpertSource:
    """The four structural invariants that make the warm-up possible at all.

    NOT a behavioural test: MQL4 does not run here, and whether an `iClose` on an
    unbuilt series actually makes the terminal fetch history is a property of
    MetaTrader that only the rig can settle. What these do guarantee is that the
    handler asks, selects, and reports, so the rig CAN settle it: the reply now
    carries `history_error`, so one live `doctor --connect` distinguishes "the
    trigger fired and 4066 came back" from "the trigger did nothing".
    """

    def body(self) -> str:
        return _function_body(
            EA_PATH.read_text(encoding="utf-8"),
            "string RatesReply(string id, string sym, string tfName, string countStr)",
        )

    def test_it_selects_the_symbol_itself(self) -> None:
        """A symbol absent from Market Watch has no series to serve.

        Every other symbol-scoped handler selects; this one did not, so `rates`
        worked only because `Engine.start()` happens to call `select` for each
        configured symbol first. That is call order, not a guarantee, and
        `/symbols add` does not go through it.
        """
        assert "SymbolSelect(sym, true)" in self.body()

    def test_it_touches_the_series_before_the_row_loop(self) -> None:
        """The early-return that made warming impossible.

        At `iBars()==0` the old handler set n=0, so the row loop never executed
        and no price-series function was reached at all. `iBars` alone is not the
        documented download trigger; the iXXX series access is. The touch must
        therefore be OUTSIDE the loop, on the path taken when there is nothing to
        return.
        """
        body = self.body()
        prologue = body[: body.index("for(int i=n-1")]
        assert "iClose(sym, tf, 0)" in prologue
        assert "iBars(sym, tf)" in prologue

    def test_it_reports_the_state_on_every_reply(self) -> None:
        body = self.body()
        for field in ("bars_total=", "selected=", "history_error="):
            assert field in body, field
        assert "GetLastError()" in body

    def test_the_wait_is_not_in_the_mailbox(self) -> None:
        """`Process()` is a single-threaded mailbox behind `gBusy` and the
        adapter's bridge times out at 5 s, so a retry loop in here would stall
        every other op and blow that timeout. The wait belongs on the desk."""
        assert "Sleep(" not in self.body()

    def test_the_timeframe_names_the_expert_knows_match_the_config(self) -> None:
        """`Tf()` falls back to PERIOD_H1 for a name it does not know, which would
        silently trade the wrong series. It is unreachable only while the two
        rosters agree, so the agreement is asserted rather than assumed."""
        from straightedge.constants import TIMEFRAME_BY_NAME

        src = EA_PATH.read_text(encoding="utf-8")
        tf = _function_body(src, "int Tf(string name)")
        for name in TIMEFRAME_BY_NAME:
            assert f'name == "{name}"' in tf, name
