from pathlib import Path

import pytest

from straightedge.config import AdviceConfig, BotConfig, load_config


# --- model pin is current generation (#15 item 5) ---------------------------
#
# Denominator: 4 shipped places name a Claude model -- the dataclass default and
# the load_config fallback in src/straightedge/config.py, plus config.example.toml
# and config.handover.toml. The handover file is the one that reaches a customer
# and it is the one that rots unseen: it sat on `claude-sonnet-4-5` while the
# example file had already moved to `claude-sonnet-5`, so the two shipped configs
# disagreed by a generation and no test looked at the handover one.
# These pins exist so a model id cannot go stale silently; when they fail, move
# every site in the list above, not only the one that failed.


def test_shipped_configs_agree_on_the_claude_model() -> None:
    """The two shipped configs must name the SAME Claude model.

    This is the gate the per-file pins above did not provide. `config.example.toml`
    moved to `claude-sonnet-5` and `config.handover.toml` stayed on
    `claude-sonnet-4-5`, a generation apart, because every pin asserted a literal
    and none compared the files to each other. The handover file is the one a
    customer runs, so it is the worst one to leave behind and the least likely to
    be read. Comparing them means a future bump cannot move one and forget the
    other, whichever direction the drift goes.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    found = {}
    for name in ("config.example.toml", "config.handover.toml"):
        text = (root / name).read_text(encoding="utf-8")
        m = re.search(r'^claude_model\s*=\s*"([^"]+)"', text, re.M)
        assert m, f"{name} does not name a claude_model"
        found[name] = m.group(1)
    assert len(set(found.values())) == 1, f"shipped configs disagree: {found}"

    from straightedge.config import AdviceConfig

    shipped = next(iter(found.values()))
    assert shipped == AdviceConfig().claude_model, (
        f"shipped configs say {shipped!r}, dataclass default says "
        f"{AdviceConfig().claude_model!r}"
    )


def test_advice_config_default_claude_pin_is_current() -> None:
    assert AdviceConfig().claude_model == "claude-opus-5-5"


def test_load_config_default_claude_pin_is_current(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[telegram]\ntoken = \"t\"\nchat_id = \"1\"\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.advice.claude_model == "claude-opus-5-5"


def test_example_config_claude_pin_is_current() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.example.toml")
    assert cfg.advice.claude_model == "claude-opus-5-5"


def test_example_config_loads() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.example.toml")
    assert cfg.mode == "paper"
    assert cfg.risk.risk_pct == 0.005
    assert "EURUSD" in cfg.symbols
    assert cfg.strategy.timeframe_id == 16385
    assert cfg.strategy.auto is False
    assert cfg.strategy.trail is False
    assert cfg.telegram.enabled is False
    assert "open" in cfg.telegram.notify_events


def test_validate_rejects_non_positive_risk() -> None:
    cfg = BotConfig()
    cfg.risk.risk_pct = 0.0
    with pytest.raises(ValueError, match="risk_pct"):
        cfg.validate()
    cfg = BotConfig()
    cfg.risk.daily_loss_pct = 0.0
    with pytest.raises(ValueError, match="daily_loss_pct"):
        cfg.validate()
    cfg = BotConfig()
    cfg.risk.max_drawdown_pct = -0.1
    with pytest.raises(ValueError, match="max_drawdown_pct"):
        cfg.validate()


def test_validate_rejects_empty_symbols_and_bad_confirm() -> None:
    cfg = BotConfig()
    cfg.symbols = []
    with pytest.raises(ValueError, match="symbol"):
        cfg.validate()
    cfg = BotConfig()
    cfg.telegram.confirm_seconds = 0
    with pytest.raises(ValueError, match="confirm_seconds"):
        cfg.validate()


def test_load_config_rejects_zero_risk_pct(tmp_path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("[risk]\nrisk_pct = 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="risk_pct"):
        load_config(path)


def test_relative_journal_and_halt_paths_anchor_to_config_dir(tmp_path: Path, monkeypatch) -> None:
    """A relative journal_path or halt_file must resolve against the config
    file's own directory, not the process working directory (fc34).

    Under Windows Task Scheduler the working directory is not the repo, so
    an operator relying on `journal_path`/`halt_file` staying near the
    config would find the journal, the lock, and the emergency HALT file
    all landing somewhere else -- silently. A HALT file created there does
    nothing: the running desk never looks in the working directory, it
    looks next to its own config.
    """
    cfg_dir = tmp_path / "cfgdir"
    cfg_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = cfg_dir / "c.toml"
    path.write_text(
        '[engine]\njournal_path = "journal.jsonl"\n'
        '[risk]\nhalt_file = "HALT"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(elsewhere)
    cfg = load_config(path)
    assert Path(cfg.journal_path) == cfg_dir / "journal.jsonl", (
        f"journal_path resolved to {cfg.journal_path!r}, "
        f"not next to the config at {cfg_dir}"
    )
    assert Path(cfg.risk.halt_file) == cfg_dir / "HALT", (
        f"halt_file resolved to {cfg.risk.halt_file!r}, "
        f"not next to the config at {cfg_dir}"
    )


def test_absolute_journal_and_halt_paths_pass_through(tmp_path: Path) -> None:
    """An operator who already pins an absolute path keeps exactly that path."""
    cfg_dir = tmp_path / "cfgdir"
    cfg_dir.mkdir()
    journal_abs = tmp_path / "state" / "journal.jsonl"
    halt_abs = tmp_path / "state" / "HALT"
    path = cfg_dir / "c.toml"
    path.write_text(
        f'[engine]\njournal_path = "{journal_abs.as_posix()}"\n'
        f'[risk]\nhalt_file = "{halt_abs.as_posix()}"\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert Path(cfg.journal_path) == journal_abs
    assert Path(cfg.risk.halt_file) == halt_abs


def test_relative_paths_with_no_config_file_anchor_to_cwd(tmp_path: Path, monkeypatch) -> None:
    """No --config at all (e.g. bare `doctor`): the documented base is CWD."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ACCOUNT_MODE", raising=False)
    cfg = load_config()
    assert Path(cfg.journal_path) == tmp_path / "journal.jsonl"
    assert Path(cfg.risk.halt_file) == tmp_path / "HALT"
