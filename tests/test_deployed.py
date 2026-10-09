"""What is running, and the two ways that question used to be unanswerable.

The live box ran twelve days at 25 commits behind `main` and nothing an
operator could see said so. `__version__` was printed by `doctor` and by nothing
else; `status_text` carried halt state, mode, server, equity, balance, peak,
positions and risk, and no version at all. So the first half of this suite is
about a desk being able to state what it is.

The second half is one line of `config.py` and it is here because it nearly
took the desk down on a real-money box. Windows PowerShell 5.1's
`Set-Content -Encoding UTF8` writes a BOM, a BOM is not valid TOML, and
`tomllib` refuses the whole file with "Invalid statement (at line 1, column 1)",
which names neither the cause nor the remedy. The config was edited that way
live on 2026-10-08 and restored from backup before the next restart. A pure
encoding artifact must not be able to refuse a config whose meaning is
unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

from straightedge import deployed, watchdog
from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig, SessionConfig, TelegramConfig, load_config
from straightedge.engine import Engine
from straightedge.synthetic import generate_bars

#: A real 40 character OID shape. Not a real commit in this repo on purpose:
#: the point is that whatever the deploy wrote is what comes back out, and a
#: test that used a live sha would start passing for the wrong reason the day
#: someone made the code resolve it.
OID = "0123456789abcdef0123456789abcdef01234567"


def _cfg(tmp_path: Path) -> BotConfig:
    cfg = BotConfig()
    cfg.mode = "mt4"
    cfg.poll_seconds = 1
    cfg.session = SessionConfig(enabled=False)
    cfg.symbols = ["EURUSD"]
    cfg.journal_path = str(tmp_path / "journal.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.telegram = TelegramConfig(token="t" * 10, chat_id="42")
    return cfg


def _engine(cfg: BotConfig, tmp_path: Path) -> Engine:
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(80, drift=0.0004, seed=3))
    engine = Engine(cfg, broker, halt_dir=str(tmp_path))
    engine.start()
    return engine


def _stamp(cfg: BotConfig, **fields: str) -> Path:
    dest = deployed.stamp_path_for(cfg.journal_path)
    dest.write_text(json.dumps(fields), encoding="utf-8")
    return dest


def _heartbeat_field(cfg: BotConfig, key: str) -> str:
    hb = watchdog.read(watchdog.heartbeat_path_for(cfg.journal_path))
    assert hb is not None, "no heartbeat was written, so this proves nothing"
    return hb.fields.get(key, "")


# --- what is running -------------------------------------------------------------------


def test_the_stamp_sits_next_to_the_journal() -> None:
    """Same idiom as the heartbeat, the lock and the inflight ledger, so one
    `journal_path` setting locates every artifact of a desk."""
    assert deployed.stamp_path_for("/x/journal.jsonl") == Path("/x/journal.deployed.json")


def test_an_unstamped_desk_says_so_rather_than_guessing(tmp_path: Path) -> None:
    """`unstamped` is an ANSWER. Silence is what twelve days of staleness was.

    It must also not fall back to `__version__`: the package version is
    hand-maintained and spans eighteen changelog sections in 1.6.0 alone, so
    reporting it as though it were the deployed commit would be a confident
    wrong answer in place of an honest absent one.
    """
    cfg = _cfg(tmp_path)
    engine = _engine(cfg, tmp_path)
    engine.step_all()
    status = engine.status_text()
    engine.stop()
    assert _heartbeat_field(cfg, "deployed") == deployed.UNSTAMPED
    assert f"deployed={deployed.UNSTAMPED}" in status
    assert deployed.describe(cfg.journal_path) == "unstamped"


def test_a_stamped_desk_names_the_ref_and_the_full_oid(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _stamp(cfg, ref="v1.6.0", commit=OID, at="2026-10-08T16:00:00+00:00")
    engine = _engine(cfg, tmp_path)
    engine.step_all()
    status = engine.status_text()
    engine.stop()
    published = _heartbeat_field(cfg, "deployed")
    print(f"deployed: heartbeat says {published!r}")
    assert published == f"v1.6.0 {OID}"
    assert OID in status and "v1.6.0" in status


def test_the_full_oid_is_published_not_an_abbreviation(tmp_path: Path) -> None:
    """An abbreviated commit is almost always unambiguous, which is a different
    kind of answer from the identifier a person can paste into `git show`."""
    cfg = _cfg(tmp_path)
    _stamp(cfg, ref="v1.6.0", commit=OID, at="")
    assert len(OID) == 40
    assert deployed.describe(cfg.journal_path).endswith(OID)


def test_a_stamp_with_no_commit_is_not_a_stamp(tmp_path: Path) -> None:
    """A ref that names no tree must not look like an answer.

    This is the failure direction that matters: `deployed=v1.6.0` with no OID
    behind it reads as a precise statement and is not one.
    """
    cfg = _cfg(tmp_path)
    _stamp(cfg, ref="v1.6.0", at="2026-10-08T16:00:00+00:00")
    assert deployed.read(cfg.journal_path) is None
    assert deployed.describe(cfg.journal_path) == deployed.UNSTAMPED


def test_a_corrupt_stamp_reads_as_unstamped_and_never_raises(tmp_path: Path) -> None:
    """A desk that refuses to start over an unparseable DIAGNOSTIC trades a
    rare ambiguity for a certain outage."""
    cfg = _cfg(tmp_path)
    deployed.stamp_path_for(cfg.journal_path).write_text("{not json", encoding="utf-8")
    assert deployed.read(cfg.journal_path) is None
    engine = _engine(cfg, tmp_path)
    engine.step_all()
    engine.stop()
    assert _heartbeat_field(cfg, "deployed") == deployed.UNSTAMPED


def test_a_stamp_written_with_a_bom_is_still_read(tmp_path: Path) -> None:
    """The deploy script runs on Windows, where a BOM is one careless cmdlet
    away. The same hazard as the config loader below, one file over."""
    cfg = _cfg(tmp_path)
    dest = deployed.stamp_path_for(cfg.journal_path)
    body = json.dumps({"ref": "v1.6.0", "commit": OID, "at": ""})
    dest.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    assert deployed.describe(cfg.journal_path) == f"v1.6.0 {OID}"


def test_a_bare_oid_with_no_ref_reports_just_the_oid(tmp_path: Path) -> None:
    """The documented interim: deploy a recorded OID when no tag names the tree."""
    cfg = _cfg(tmp_path)
    _stamp(cfg, ref="", commit=OID, at="")
    assert deployed.describe(cfg.journal_path) == OID


# --- the BOM that nearly stopped a real-money desk -------------------------------------


def _minimal_toml() -> str:
    """Smallest config that proves the parse reached real values.

    `poll_seconds` sits under `[engine]` and `risk_pct` under `[risk]`, so a
    BOM that broke the parse at line 1 column 1 could not leave either of them
    at a default by accident: both come back wrong together or not at all.
    """
    return "[engine]\npoll_seconds = 1\n\n[risk]\nrisk_pct = 0.005\n"


def test_a_utf8_bom_does_not_stop_the_desk_and_is_reported(tmp_path: Path, capsys) -> None:
    """Measured live 2026-10-08 on a real-money box.

    `Set-Content -Encoding UTF8` on Windows PowerShell 5.1 wrote a BOM and
    `tomllib` refused the file with "Invalid statement (at line 1, column 1)".
    The desk would not have started at its next restart. Reverting the decode
    to plain `utf-8` drives this test red with that exact exception, which is
    how it was checked.
    """
    dest = tmp_path / "config.toml"
    dest.write_bytes(b"\xef\xbb\xbf" + _minimal_toml().encode("utf-8"))
    cfg = load_config(dest)
    assert cfg.poll_seconds == 1
    assert cfg.risk.risk_pct == 0.005
    err = capsys.readouterr().err
    print(f"config: BOM warning -> {err.strip()[:80]}")
    assert "UTF-8 BOM" in err
    assert "tolerated" in err
    # The remedy is named, not just the fault. An operator who hits this is on
    # a Windows box mid-deploy and should not have to go looking.
    assert "Set-Content" in err


def test_a_config_without_a_bom_produces_no_warning(tmp_path: Path, capsys) -> None:
    """The positive control for the test above: the warning has to be able NOT
    to fire, or it is noise on every load and gets filtered out."""
    dest = tmp_path / "config.toml"
    dest.write_text(_minimal_toml(), encoding="utf-8")
    cfg = load_config(dest)
    assert cfg.poll_seconds == 1
    assert "BOM" not in capsys.readouterr().err


def test_a_bom_is_tolerated_and_the_file_is_otherwise_unchanged(tmp_path: Path) -> None:
    """Tolerating the BOM must not change how anything after it is parsed."""
    body = _minimal_toml()
    plain = tmp_path / "plain.toml"
    plain.write_text(body, encoding="utf-8")
    bommed = tmp_path / "bommed.toml"
    bommed.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    a = load_config(plain)
    b = load_config(bommed)
    assert a.poll_seconds == b.poll_seconds
    assert a.risk.risk_pct == b.risk.risk_pct
    assert a.mode == b.mode


def test_a_stamp_that_is_valid_json_but_not_an_object_is_not_a_stamp(tmp_path: Path) -> None:
    """`json.loads` accepts a list, a bare string and a number.

    `read` has to reject them by SHAPE, not by catching an exception, because
    none of them raises. A `[]` here would otherwise reach `.get` and fail with
    an AttributeError inside the desk's heartbeat write, which is a diagnostic
    taking down a tick.
    """
    cfg = _cfg(tmp_path)
    dest = deployed.stamp_path_for(cfg.journal_path)
    for body in ("[]", '"v1.6.0"', "42", "null"):
        dest.write_text(body, encoding="utf-8")
        assert deployed.read(cfg.journal_path) is None, body
        assert deployed.describe(cfg.journal_path) == deployed.UNSTAMPED, body


# --- the deploy's own config gate ------------------------------------------------------


def _load_asserter():
    """Import `deploy/windows/assert-config-loads.py` by path.

    It is a deploy helper rather than a CLI subcommand on purpose, so it has a
    hyphen in its name and is not importable as a module. It still gets tested,
    because it is the step that stands between a careless Windows text edit and
    a desk that will not start.
    """
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "deploy" / "windows" / "assert-config-loads.py"
    spec = importlib.util.spec_from_file_location("se_assert_config_loads", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_config_gate_passes_a_good_file_and_prints_the_derived_figures(
    tmp_path: Path, capsys
) -> None:
    dest = tmp_path / "config.toml"
    dest.write_text(_minimal_toml(), encoding="utf-8")
    code = _load_asserter().main([str(dest)])
    out = capsys.readouterr().out
    assert code == 0
    # The figures a deploy and the supervision audit both need, from the one
    # place that derives them.
    for key in ("config loaded", "stale_after_s", "restart interval", "start is notified"):
        assert key in out, key


def test_the_config_gate_refuses_a_file_the_loader_cannot_read(
    tmp_path: Path, capsys
) -> None:
    """The positive control: it has to be able to fail, or step 6 of the deploy
    is decoration."""
    dest = tmp_path / "config.toml"
    dest.write_text("this is not toml = = =\n", encoding="utf-8")
    code = _load_asserter().main([str(dest)])
    err = capsys.readouterr().err
    assert code == 2
    assert "did NOT load" in err


def test_the_config_gate_refuses_a_missing_file(tmp_path: Path, capsys) -> None:
    code = _load_asserter().main([str(tmp_path / "nope.toml")])
    assert code == 2
    assert "no such config file" in capsys.readouterr().err


def test_the_config_gate_prints_notify_event_names_and_no_other_config_line(
    tmp_path: Path, capsys
) -> None:
    """`config.toml` carries credentials (se#90), so this gate prints a NAMED
    subset and never the file.

    `start` being in `notify_events` is the thing worth reading here: it decides
    whether a restart announces itself at all, and it was the measurement that
    stopped a feature being built twice.
    """
    dest = tmp_path / "config.toml"
    dest.write_text(
        _minimal_toml() + '\n[telegram]\ntoken = "SHOULD-NOT-APPEAR"\nchat_id = "42"\n',
        encoding="utf-8",
    )
    code = _load_asserter().main([str(dest)])
    captured = capsys.readouterr()
    assert code == 0
    assert "SHOULD-NOT-APPEAR" not in captured.out
    assert "SHOULD-NOT-APPEAR" not in captured.err
    assert "start is notified  : True" in captured.out
