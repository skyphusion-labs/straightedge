"""The broker account login must not reach a reader's screen. straightedge#90.

What this gate covers
---------------------
A broker login is an account IDENTIFIER, not a credential: the password and the
server are separate and neither is journaled. So this is not an exposure and
nothing rotates. It is a visibility defect, and the thing that makes it one is
that `SECURITY.md` lists `MT5_LOGIN` under "Secret names" while four separate
paths put it in front of a reader.

FOUR sinks, measured at `main` 9716445 before the fix. A fix covering one of
them looks complete, which is why each sink gets its own test here:

  1. `journal.jsonl`. `engine.py:270` writes `login=acct.login` into every
     `start` record and `journal._SECRET_KEYS` did not list `login`.
  2. stdout. `__main__.py:328` and `:368` print `login=` on `doctor --connect`.
  3. Telegram. NOT through the `start` notify text, which `_format_event`
     builds without `login`. Through `engine.history_text()`, which renders
     EVERY field of every journal row it reads, served by `/history` and by the
     daily `recap` (an event in the shipped `notify_events`).
  4. An external AI provider. `engine.advice_history()` is `journal.tail(40)`
     and `llm._computer` POSTs it as the `history` field of the advice request,
     so with the shipped `AI_PROVIDER=computer` a `/ask` turn taken soon after a
     start sends the row off the box.

Sinks 3 and 4 both read through `Journal.tail()`, and they read rows that are
ALREADY on disk. An append-only audit log is never rewritten, so redacting only
on write would leave every pre-existing `start` row readable through both: the
live box had 8 of them. `tail()` therefore redacts on READ as well. The
`last_event()` path deliberately does NOT, because it restores a pending order
rather than showing anything to anyone.

Proven red: with `"login"` removed from `_SECRET_KEYS`, tests 1, 2 and 3 fail;
with `redact()` dropped from `tail()`, tests 2 and 3 fail; with either
`mask_account_id` call in `__main__.py` reverted to `acct.login`, the matching
`doctor` test fails. A gate seen only green is not a gate.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from straightedge.models import VenueClock
from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import Engine
from straightedge.journal import Journal, mask_account_id
from straightedge.synthetic import generate_bars

# Eight digits, the length a real MT4/MT5 login actually has. The fakes in
# `test_cli.py` use 1 and 42, which are too short to mask (see
# `test_mask_refuses_to_partially_mask_a_short_id`), so they pin the refusal
# and this file pins the masked shape.
LOGIN = 51234567
LOGIN_S = "51234567"


def _raw_start_row(path: Path) -> None:
    """A `start` row exactly as the code wrote it BEFORE the fix.

    Written by hand rather than through `Journal.write`, because the point is a
    row that is already on disk when the redacting build starts reading.
    """
    rec = {
        "ts": "2026-10-08T12:00:00+00:00",
        "event": "start",
        "mode": "mt5",
        "login": LOGIN,
        "equity": 10000.0,
        "server": "Demo-Server",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _engine(tmp_path: Path) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    return Engine(
        cfg,
        broker,
        halt_dir=str(tmp_path),
        now_fn=lambda: datetime(2024, 1, 3, 12, tzinfo=timezone.utc),
    )


# --- sink 1: the journal file -------------------------------------------------


def test_journal_write_redacts_login(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    Journal(path).write("start", mode="mt5", login=LOGIN, equity=10_000.0)
    text = path.read_text(encoding="utf-8")
    assert LOGIN_S not in text
    rec = json.loads(text.splitlines()[0])
    assert rec["login"] == "[REDACTED]"
    # The row still identifies the session. Redacting the identifier must not
    # cost the fields an audit reads it for.
    assert rec["mode"] == "mt5"
    assert rec["equity"] == 10_000.0


# --- sinks 3 and 4: rows already on disk, read back -------------------------


def test_tail_redacts_a_login_written_before_the_fix(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    _raw_start_row(path)
    assert LOGIN_S in path.read_text(encoding="utf-8")  # the instrument can see it
    rec = Journal(path).tail(10)[0]
    assert rec["login"] == "[REDACTED]"
    assert LOGIN_S not in json.dumps(rec)
    assert rec["server"] == "Demo-Server"


def test_advice_history_sent_to_the_provider_carries_no_login(tmp_path: Path) -> None:
    """Sink 4. `advice_history()` is the POST body's `history` field."""
    engine = _engine(tmp_path)
    _raw_start_row(Path(engine.cfg.journal_path))
    rows = engine.advice_history(40)
    assert rows, "no rows read: the test would pass on an empty list"
    assert LOGIN_S not in json.dumps(rows)


def test_history_text_carries_no_login(tmp_path: Path) -> None:
    """Sink 3. What `/history` and the daily `recap` put in the chat."""
    engine = _engine(tmp_path)
    _raw_start_row(Path(engine.cfg.journal_path))
    text = engine.history_text(10)
    assert "start" in text, "the row was not rendered: assertion below is vacuous"
    assert LOGIN_S not in text


# --- sink 2: the operator's own console --------------------------------------


def test_doctor_connect_mt5_masks_login(capsys, monkeypatch) -> None:
    from straightedge.__main__ import main

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("ACCOUNT_MODE", raising=False)

    class FakeBroker:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def ensure_connected(self) -> None:
            return None

        def disconnect(self) -> None:
            return None

        def account(self):
            return type(
                "Acct",
                (),
                {
                    "login": LOGIN,
                    "server": "Demo-Server",
                    "equity": 10000.0,
                    "currency": "USD",
                    "trade_mode": 0,
                },
            )()

        def rates(self, name, timeframe, count):
            del name, timeframe
            return generate_bars(max(int(count), 80), drift=0.0002, seed=7)

        # A venue has to be able to state its clock (straightedge#172).
        # Zero is this fake stamping UTC, which keeps THIS test's
        # subject unchanged; the clock's own suite is
        # tests/test_venue_clock.py.
        def venue_clock(self, name):
            del name
            return VenueClock(offset_sec=0, source="fake")

    monkeypatch.setattr("straightedge.broker.mt5_live.load_mt5_module", lambda: object())
    monkeypatch.setattr("straightedge.broker.mt5_live.Mt5Broker", lambda **kw: FakeBroker(**kw))
    assert main(["doctor", "--connect"]) == 0
    out = capsys.readouterr().out
    assert "connected login=" in out, "the print site did not run"
    assert LOGIN_S not in out
    assert "login=***4567" in out
    assert "server=Demo-Server" in out


def test_doctor_connect_mt4_masks_login(capsys, monkeypatch, tmp_path) -> None:
    from straightedge.__main__ import main

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("ACCOUNT_MODE", "mt4")
    monkeypatch.setenv("MT4_FILES_DIR", str(tmp_path))

    class FakeMt4Broker:
        def __init__(self, call, **kwargs) -> None:
            del call, kwargs

        def connect(self) -> None:
            return None

        def disconnect(self) -> None:
            return None

        def account(self):
            return type(
                "Acct",
                (),
                {
                    "login": LOGIN,
                    "server": "MT4-Demo",
                    "equity": 10000.0,
                    "currency": "USD",
                    "trade_mode": 0,
                },
            )()

        def rates(self, name, timeframe, count):
            del name, timeframe
            return generate_bars(max(int(count), 80), drift=0.0002, seed=7)

        # A venue has to be able to state its clock (straightedge#172).
        # Zero is this fake stamping UTC, which keeps THIS test's
        # subject unchanged; the clock's own suite is
        # tests/test_venue_clock.py.
        def venue_clock(self, name):
            del name
            return VenueClock(offset_sec=0, source="fake")

    monkeypatch.setattr("straightedge.broker.mt4_live.Mt4Broker", FakeMt4Broker)
    assert main(["doctor", "--connect"]) == 0
    out = capsys.readouterr().out
    assert "connected venue=mt4 login=" in out, "the print site did not run"
    assert LOGIN_S not in out
    assert "login=***4567" in out


# --- the mask itself ----------------------------------------------------------


def test_mask_keeps_the_last_four_and_a_fixed_prefix() -> None:
    # Fixed "***", not one star per hidden digit: the number of stars would
    # publish the login's length for nothing.
    assert mask_account_id(51234567) == "***4567"
    assert mask_account_id("51234567") == "***4567"
    assert mask_account_id(123456789012) == "***9012"


def test_mask_refuses_to_partially_mask_a_short_id() -> None:
    # Fewer than six digits. A real MT4/MT5 login is six or more, and below
    # that the last four leave too little hidden to be a mask at all, so the
    # honest answer is to show nothing rather than most of it.
    for value in (1, 42, 4567, 12345, "", None):
        assert mask_account_id(value) == "[REDACTED]"
