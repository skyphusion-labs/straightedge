"""Which secrets came from config.toml is REPORTED, never guessed. #139.

The contradiction this closes
-----------------------------
`SECURITY.md` said "Secrets live in the environment. Do not put secrets in
`config.toml`." The loader accepts nine of those keys from the file when their
variable is unset, and `SECURITY.md:34` conceded it two paragraphs later ("Env
vars override toml if both are set"). On a public real-money repo the document
should not read as a control the code does not have.

Why the doc moved and the code did not
--------------------------------------
Failing closed at load is the only change that would make the old sentence
true, and it would stop a running desk from starting: the demo box has a
`[telegram]` section holding its bot token. So the fallback stays and the gap
is closed by OBSERVABILITY, which is the same trade `docs/CONTRACT.md` already
records for the handover posture: "the gap that leaves is closed by
observability, not by a stricter default" (flipping the default would silently
change every existing deployment).

Two reports, and they are different facts:

* `settings_from_file` -- keys the loader TOOK from the file. A supported
  fallback. The empty case is printed too, because a line that appears only
  when something is wrong cannot be told apart from a line nobody wrote.
* `settings_read_from_nowhere` -- keys present in the file that the loader reads
  from NOWHERE (today only `mt4.mailbox_token`). This is the worse one: an
  operator instruction being discarded, which is the defect
  `parse_symbol_deviation_points` already names, "nothing reports the
  disagreement".

Not refused, and that is load-bearing
-------------------------------------
`mt4.mailbox_token` is NOT refused at load. The only state where ignoring it is
dangerous is `mailbox_url` set with no `MT4_MAILBOX_TOKEN`, and
`mt4_net.require_token` already fails closed there, at startup, on both ends.
A load-time gate would catch nothing that one misses and would stop two
WORKING configurations: the variable set with a stale key left in the file, and
the co-located file mailbox, which needs no token at all.
`test_an_ignored_key_is_reported_not_refused` pins that, and
`test_the_dangerous_state_is_still_fail_closed_one_layer_down` shows the gate
that does the refusing, so "not refused here" cannot be read as "not refused".

NO TEST IN THIS FILE MAY PRINT A SECRET VALUE. Each asserts the key NAME is
reported AND the value is absent, which is also what proves the assertions are
not vacuous: a report that said nothing would fail the first half.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from straightedge.config import (
    FILE_SOURCED_SETTINGS,
    IGNORED_FILE_SOURCED_SETTINGS,
    load_config,
    settings_read_from_nowhere,
    settings_taken_from_file,
)

# Shaped like the real thing and long enough to be unmistakable in a diff, but
# never a live credential and never printed by an assertion.
FAKE_TG_TOKEN = "123456789:FAKE-TOKEN-DO-NOT-USE-aaaaaaaaaaaaaaaa"
FAKE_XAI_KEY = "xai-FAKE-KEY-DO-NOT-USE-bbbbbbbbbbbbbbbb"
FAKE_MAILBOX_TOKEN = "FAKE-MAILBOX-TOKEN-DO-NOT-USE-cccccccccccccccc"

SECRET_ENV_VARS = tuple(var for var, _, _ in FILE_SOURCED_SETTINGS) + ("MT4_MAILBOX_TOKEN",)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No inherited secret may decide any test here.

    Without this an exported TELEGRAM_BOT_TOKEN on the developer's box would
    make the file lose, and the test would pass for the wrong reason.
    """
    for var in SECRET_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ACCOUNT_MODE", raising=False)


def _write(tmp_path: Path, body: str) -> str:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


# --- the accepted fallback ----------------------------------------------------


def test_a_secret_in_the_file_is_reported_by_name(tmp_path: Path, clean_env: None) -> None:
    path = _write(
        tmp_path,
        f'[telegram]\ntoken = "{FAKE_TG_TOKEN}"\nchat_id = "4242"\n'
        f'[advice]\ngrok_key = "{FAKE_XAI_KEY}"\n',
    )
    cfg = load_config(path)
    assert cfg.settings_from_file == ("telegram.token", "telegram.chat_id", "advice.grok_key")
    # The loader still USES them: this is a report, not a behaviour change.
    assert cfg.telegram.token == FAKE_TG_TOKEN
    assert cfg.advice.grok_key == FAKE_XAI_KEY
    # And no value leaked into the report itself.
    assert FAKE_TG_TOKEN not in str(cfg.settings_from_file)
    assert FAKE_XAI_KEY not in str(cfg.settings_from_file)


def test_the_environment_wins_and_is_not_reported(
    tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, f'[telegram]\ntoken = "{FAKE_TG_TOKEN}"\nchat_id = "4242"\n')
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-the-environment")
    cfg = load_config(path)
    assert cfg.telegram.token == "from-the-environment"
    # chat_id still came from the file, so the report must be PARTIAL rather
    # than all-or-nothing. A report that collapsed to empty here would hide a
    # real file-sourced secret.
    assert cfg.settings_from_file == ("telegram.chat_id",)


def test_an_empty_environment_variable_still_wins(
    tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact semantics of `os.environ.get(VAR, <file>)`, preserved.

    An exported empty string beats the file today and disables the feature.
    Reading it as "unset" would be a behaviour change on a running desk,
    arriving inside a change whose whole point is to report rather than alter.
    """
    path = _write(tmp_path, f'[telegram]\ntoken = "{FAKE_TG_TOKEN}"\n')
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    cfg = load_config(path)
    assert cfg.telegram.token == ""
    assert cfg.settings_from_file == ()


def test_all_from_the_environment_is_the_empty_tuple(tmp_path: Path, clean_env: None) -> None:
    cfg = load_config(_write(tmp_path, '[account]\nmode = "paper"\n'))
    assert cfg.settings_from_file == ()
    assert cfg.settings_read_from_nowhere == ()


def test_an_empty_value_in_the_file_is_not_a_file_secret(
    tmp_path: Path, clean_env: None
) -> None:
    """`token = ""` is the shape config.example.toml tells operators to leave.

    Reporting it would make the line cry wolf on the recommended config.
    """
    cfg = load_config(_write(tmp_path, '[telegram]\ntoken = ""\n[mt5]\npassword = ""\n'))
    assert cfg.settings_from_file == ()


# --- the ignored key ----------------------------------------------------------


def test_an_ignored_key_is_reported_not_refused(tmp_path: Path, clean_env: None) -> None:
    """`mt4.mailbox_token` is read from nowhere. Say so; do not refuse."""
    path = _write(
        tmp_path,
        f'[account]\nmode = "mt4"\n[mt4]\nmailbox_token = "{FAKE_MAILBOX_TOKEN}"\n',
    )
    cfg = load_config(path)  # must NOT raise
    assert cfg.mt4.mailbox_token == "", "the file value was read; it must not be"
    assert cfg.settings_read_from_nowhere == ("mt4.mailbox_token",)
    assert FAKE_MAILBOX_TOKEN not in str(cfg.settings_read_from_nowhere)


def test_an_ignored_key_is_reported_even_when_the_variable_is_set(
    tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is wrong regardless of the environment.

    Unlike `settings_from_file`, this report is not about which source won. The
    key is read from nowhere, so a variable happening to be set does not make
    the line in the file any less discarded.
    """
    path = _write(tmp_path, f'[mt4]\nmailbox_token = "{FAKE_MAILBOX_TOKEN}"\n')
    monkeypatch.setenv("MT4_MAILBOX_TOKEN", "x" * 40)
    cfg = load_config(path)
    assert cfg.settings_read_from_nowhere == ("mt4.mailbox_token",)


def test_the_dangerous_state_is_still_fail_closed_one_layer_down() -> None:
    """Why #139 adds no gate: the gate already exists, and here it is.

    `mailbox_url` set with no token is the only state in which an ignored
    `mt4.mailbox_token` could put an unauthenticated order endpoint in front of
    a desk, and `mt4_net.require_token` refuses it at startup. Without this
    test, "reported, not refused" would read as "nobody refuses it".
    """
    from straightedge.broker.mt4_net import require_token

    with pytest.raises(RuntimeError) as empty:
        require_token("", where="mt4.mailbox_url is set but")
    assert "no unauthenticated mode" in str(empty.value)

    with pytest.raises(RuntimeError) as short:
        require_token("tiny", where="mt4.mailbox_url is set but")
    assert "minimum" in str(short.value)


# --- the doc and the loader cannot drift again --------------------------------


def test_security_md_names_exactly_the_keys_the_loader_accepts() -> None:
    """Both directions. The issue was one side describing the other wrongly.

    Checked against the ENV VAR names, because that is the vocabulary
    SECURITY.md's "Secret names" list uses.
    """
    text = (Path(__file__).resolve().parents[1] / "SECURITY.md").read_text(encoding="utf-8")
    for var, section, key in FILE_SOURCED_SETTINGS:
        assert f"`{var}`" in text, f"{var} is accepted from the file and SECURITY.md omits it"
        assert f"`{section}.{key}`" in text, (
            f"SECURITY.md does not name the TOML key {section}.{key} that the loader reads"
        )
    for section, key in IGNORED_FILE_SOURCED_SETTINGS:
        assert f"`{section}.{key}`" in text, (
            f"SECURITY.md does not name the ignored key {section}.{key}"
        )
    # The sentence that was false. It must not come back.
    assert "Do not put secrets in `config.toml`." not in text, (
        "SECURITY.md has regained a claim the loader does not enforce"
    )


def test_every_accepted_key_is_actually_reported(tmp_path: Path, clean_env: None) -> None:
    """All nine at once, so none can be reported by accident or forgotten.

    Built FROM `FILE_SOURCED_SETTINGS` rather than hand-listed, so a key added to
    that tuple without a loader site fails here instead of passing quietly.
    TOML forbids a repeated table header, so the sections are grouped; and
    `mt5.login` is written as an integer because the loader runs it through
    `int()`.
    """
    grouped: dict[str, list[str]] = {}
    for _, section, key in FILE_SOURCED_SETTINGS:
        value = "51234567" if key == "login" else '"filler-value"'
        grouped.setdefault(section, []).append(f"{key} = {value}")
    body = "".join(
        f"[{section}]\n" + "".join(f"{line}\n" for line in keys)
        for section, keys in grouped.items()
    )
    cfg = load_config(_write(tmp_path, body))
    expected = tuple(f"{section}.{key}" for _, section, key in FILE_SOURCED_SETTINGS)
    assert cfg.settings_from_file == expected
    assert len(expected) == 9, "FILE_SOURCED_SETTINGS changed; SECURITY.md must change with it"
    assert "filler-value" not in str(cfg.settings_from_file)


def test_helpers_are_pure_functions_of_their_inputs() -> None:
    assert settings_taken_from_file({"telegram": {"token": "t"}}, environ={}) == (
        "telegram.token",
    )
    assert settings_taken_from_file(
        {"telegram": {"token": "t"}}, environ={"TELEGRAM_BOT_TOKEN": "e"}
    ) == ()
    assert settings_read_from_nowhere({"mt4": {"mailbox_token": "t"}}) == ("mt4.mailbox_token",)
    assert settings_read_from_nowhere({"mt4": {"mailbox_token": ""}}) == ()


# --- the two sinks that show it to a human -----------------------------------


def test_doctor_names_the_keys_and_never_the_values(
    tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The operator's own console. Key names, values absent.

    Both halves matter: without the first the report could be empty and the
    value assertion would pass vacuously, and without the second this change
    would have invented a new leak while closing a documentation gap.
    """
    from straightedge.__main__ import main

    path = _write(
        tmp_path,
        f'[telegram]\ntoken = "{FAKE_TG_TOKEN}"\nchat_id = "4242"\n'
        f'[advice]\ngrok_key = "{FAKE_XAI_KEY}"\n'
        f'[mt4]\nmailbox_token = "{FAKE_MAILBOX_TOKEN}"\n',
    )
    # rc is 1, and that is the POSITIVE CONTROL for this whole change: the
    # ping failed because the desk actually USED the token it read out of the
    # file. A green doctor here would mean the file-sourced token never
    # reached the Telegram client and the report was describing nothing.
    assert main(["--config", path, "doctor"]) == 1
    out = capsys.readouterr().out
    assert "telegram ping: fail" in out

    assert "secrets: 3 from config.toml" in out
    for name in ("telegram.token", "telegram.chat_id", "advice.grok_key"):
        assert name in out
    assert "secrets IGNORED in config.toml: mt4.mailbox_token" in out

    # The four presence lines report the EFFECTIVE value and its source. These
    # read "unset" before straightedge#139 even though the desk had the token.
    assert "telegram token: SET (config.toml)" in out
    assert "telegram chat: SET (config.toml)" in out
    assert "xai key: SET (config.toml)" in out
    # The claude credential line is labelled by FUNCTION, not by provider, because
    # `advice.claude_key` carries an Anthropic key on the direct URL and a
    # Cloudflare token when `claude_url` points at an AI Gateway. This config
    # leaves `claude_url` at its default, so the direct wording is the one to see.
    # The gateway wording is asserted in test_doctor_labels_a_gateway_credential.
    assert "claude credential (anthropic key): unset" in out
    assert "anthropic key:" not in out, "stale provider-shaped label is back"

    for value in (FAKE_TG_TOKEN, FAKE_XAI_KEY, FAKE_MAILBOX_TOKEN):
        assert value not in out, "doctor printed a secret VALUE"


def test_doctor_labels_a_gateway_credential_by_function(
    tmp_path: Path, clean_env: None, capsys
) -> None:
    """With an AI Gateway URL, doctor must not call the token an Anthropic key.

    THIS TEST DRIVES DOCTOR. An earlier version of it asserted `_is_cf_gateway`
    directly, took `tmp_path` and `capsys` and used neither, and therefore left
    exactly the state its own docstring promised to prevent: with the label
    hardcoded to the anthropic wording in BOTH branches, both doctor tests still
    passed. A test that names a guarantee it does not provide is worse than no
    test, because it is counted.

    The pre-live check is the one screen an operator reads to find out what the
    desk thinks it holds. Calling a Cloudflare token "anthropic key" there is
    wrong by function and wrong in the expensive direction: it sends them looking
    for an Anthropic account they do not need.
    """
    from straightedge.__main__ import main

    path = _write(
        tmp_path,
        '[account]\nmode = "paper"\n\n[advice]\n'
        'claude_url = "https://gateway.ai.cloudflare.com/v1/a/g/anthropic/v1/messages"\n'
        'claude_key = "cf-token-value"\n',
    )
    main(["--config", path, "doctor"])
    out = capsys.readouterr().out
    assert "claude credential (cloudflare gateway token): SET" in out
    assert "anthropic key" not in out, "a gateway token labelled as an Anthropic key"
    assert "cf-token-value" not in out, "doctor printed a secret VALUE"


def test_doctor_names_the_environment_as_the_source_when_it_wins(
    tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The other arm of `_presence`. Without it, "SET (config.toml)" above
    could be a constant string rather than a measurement."""
    from straightedge.__main__ import main

    monkeypatch.setenv("XAI_API_KEY", "from-the-environment")
    path = _write(tmp_path, '[account]\nmode = "paper"\n')
    main(["--config", path, "doctor"])
    out = capsys.readouterr().out
    assert "xai key: SET (env)" in out
    assert "from-the-environment" not in out


def test_doctor_says_so_when_every_secret_came_from_the_environment(
    tmp_path: Path, clean_env: None, capsys
) -> None:
    """The empty case is stated, not left to silence.

    A line that only appears when something is wrong is indistinguishable from
    a line nobody implemented, which is the failure mode `doctor` already
    refuses with "history: NOT MEASURED".
    """
    from straightedge.__main__ import main

    path = _write(tmp_path, '[account]\nmode = "paper"\n')
    assert main(["--config", path, "doctor"]) == 0
    out = capsys.readouterr().out
    assert "secrets: all from the environment" in out
    assert "IGNORED" not in out


def test_the_start_record_carries_the_provenance(tmp_path: Path) -> None:
    """Readable after the fact, for the same reason as the handover posture.

    Asserted on the JOURNAL FILE, not on the config object: the point is that a
    reader of the journal can tell which source a past session used.
    """
    import json
    from datetime import datetime, timezone

    from straightedge.broker.paper import PaperBroker
    from straightedge.config import BotConfig
    from straightedge.engine import Engine
    from straightedge.synthetic import generate_bars

    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.settings_from_file = ("telegram.token",)
    cfg.settings_read_from_nowhere = ("mt4.mailbox_token",)
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    engine = Engine(
        cfg,
        broker,
        halt_dir=str(tmp_path),
        now_fn=lambda: datetime(2024, 1, 3, 12, tzinfo=timezone.utc),
    )
    engine.start()
    engine.stop()

    rows = [json.loads(x) for x in Path(cfg.journal_path).read_text().splitlines() if x.strip()]
    start = [r for r in rows if r.get("event") == "start"]
    assert start, "no start row was written: the assertions below would be vacuous"
    assert start[0]["settings_from_file"] == ["telegram.token"]
    assert start[0]["settings_read_from_nowhere"] == ["mt4.mailbox_token"]
