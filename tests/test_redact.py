import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from wincompat import assert_owner_mode, assert_same_path
from straightedge.journal import (
    InstanceLock,
    InstanceLockError,
    Journal,
    lock_path_for,
    redact_text,
)

FAKE_TOKEN = "123456789:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
FAKE_PASSWORD = "s3cr3t-pass-value"


def test_journal_write_redacts_password_and_token(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    Journal(path).write("auth", password=FAKE_PASSWORD, token=FAKE_TOKEN, ok=True)
    text = path.read_text(encoding="utf-8")
    assert FAKE_PASSWORD not in text
    assert FAKE_TOKEN not in text
    rec = Journal(path).tail(1)[0]
    assert rec["event"] == "auth"
    assert rec["ok"] is True
    assert rec["password"] != FAKE_PASSWORD
    assert rec["token"] != FAKE_TOKEN


def test_journal_write_redacts_named_keys_and_keeps_safe_fields(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    Journal(path).write(
        "keys",
        api_key="sk-test-api-key-value",
        grok_key="xai-test-grok-key",
        claude_key="sk-ant-test-claude-key",
        symbol="EURUSD",
    )
    text = path.read_text(encoding="utf-8")
    assert "sk-test-api-key-value" not in text
    assert "xai-test-grok-key" not in text
    assert "sk-ant-test-claude-key" not in text
    assert "EURUSD" in text


def test_journal_write_redacts_nested_and_embedded_token(tmp_path: Path) -> None:
    @dataclass
    class Creds:
        password: str
        login: int

    path = tmp_path / "j.jsonl"
    Journal(path).write(
        "nested",
        creds=Creds(password="nested-secret-pass", login=42),
        items=[{"password": "list-secret", "n": 1}],
        error=f"telegram http failed token={FAKE_TOKEN}",
    )
    text = path.read_text(encoding="utf-8")
    assert "nested-secret-pass" not in text
    assert "list-secret" not in text
    assert FAKE_TOKEN not in text
    rec = Journal(path).tail(1)[0]
    # straightedge#90 added `login` to `_SECRET_KEYS`, so a nested one is now
    # redacted too. This line read `== 42` and was the pin that said otherwise.
    # `n` keeps the other half honest: redaction is still key-named, not a
    # blanket over every nested field.
    assert rec["creds"]["login"] == "[REDACTED]"
    assert rec["items"][0]["n"] == 1
    assert FAKE_TOKEN not in rec["error"]


def test_redact_text_strips_bot_token_keeps_rest() -> None:
    msg = f"loop error: telegram http failed token={FAKE_TOKEN} ok"
    out = redact_text(msg)
    assert FAKE_TOKEN not in out
    assert "[REDACTED]" in out
    assert "loop error" in out


def test_redact_text_strips_botfather_token() -> None:
    secret = "1234567890:AA" + "x" * 35
    out = redact_text("token " + secret + " leftover")
    assert secret not in out
    assert "leftover" in out
    assert "[REDACTED]" in out


def test_journal_file_is_0600_after_write(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    Journal(path).write("ping")
    assert_owner_mode(path)


def test_lock_path_for_uses_journal_stem() -> None:
    assert_same_path(lock_path_for("journal.jsonl"), "journal.lock")
    assert_same_path(lock_path_for("/tmp/desk.jsonl"), "/tmp/desk.lock")


def test_instance_lock_file_is_0600(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    lock = InstanceLock(journal)
    lock.acquire()
    try:
        path = lock_path_for(journal)
        assert path.exists()
        assert_owner_mode(path)
    finally:
        lock.release()


def test_instance_lock_blocks_other_process(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    held = InstanceLock(journal)
    held.acquire()
    try:
        src = str(Path(__file__).resolve().parents[1] / "src")
        env = os.environ.copy()
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from straightedge.journal import InstanceLock, InstanceLockError\n"
                f"try:\n"
                f"    InstanceLock({str(journal)!r}).acquire()\n"
                "except InstanceLockError as exc:\n"
                "    print(exc)\n"
                "    raise SystemExit(2)\n"
                "raise SystemExit(0)\n",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert proc.returncode == 2
        assert "already running" in proc.stdout
    finally:
        held.release()


def test_instance_lock_acquire_raises_when_flock_blocks(tmp_path: Path, monkeypatch) -> None:
    journal = tmp_path / "journal.jsonl"

    def blocked(*_args, **_kwargs):
        raise BlockingIOError("locked")

    monkeypatch.setattr("straightedge.journal._lock_nb", blocked)
    lock = InstanceLock(journal)
    try:
        lock.acquire()
    except InstanceLockError as exc:
        assert "already running" in str(exc)
        assert lock._fh is None
        return
    raise AssertionError("expected InstanceLockError")


def test_instance_lock_context_manager_and_double_release(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    with InstanceLock(journal) as lock:
        assert lock_path_for(journal).exists()
        assert lock._fh is not None
    assert lock._fh is None
    lock.release()
