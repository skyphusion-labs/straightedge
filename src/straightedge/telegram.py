"""Telegram Bot API client. Stdlib only. Token never logged.

Alerts and operator commands. Unauthorized chat ids are ignored.
Transport is injectable so tests never hit api.telegram.org.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from straightedge.config import TelegramConfig, is_shared_chat_id
from straightedge.journal import redact_text

API_ROOT = "https://api.telegram.org"
RETRY_TRIES = 4
RETRY_CAP_S = 60.0
#: Added to a long-poll timeout to get the HTTP timeout, so the SERVER is the
#: end that gives up first and a bare socket timeout does not get read as "no
#: updates". Named because `straightedge.watchdog` derives the tick budget from
#: it: a staleness threshold has to know how long one tick may legitimately
#: wait, and a literal 5 copied into that module would drift from this one.
POLL_TIMEOUT_MARGIN_S = 5.0


class Transport(Protocol):
    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        timeout: float = 10.0,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...


class UrlLibTransport:
    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        timeout: float = 10.0,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        hdrs = {
            "Content-Type": "application/json",
            "User-Agent": "straightedge/1.0",
        }
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(
            url,
            data=body,
            headers=hdrs,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from None
        except urllib.error.URLError:
            raise TelegramError("telegram http failed") from None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TelegramError("telegram non-json") from exc
        if not isinstance(data, dict):
            raise TelegramError("telegram bad payload")
        return data


class TelegramError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def retryable_status(status: int | None) -> bool:
    if status == 429:
        return True
    return status is not None and 500 <= status <= 599


def backoff_seconds(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None and retry_after > 0:
        return min(float(retry_after), RETRY_CAP_S)
    return min(0.5 * (2 ** max(attempt, 0)), RETRY_CAP_S)


def _retry_after_from(data: dict[str, Any]) -> float | None:
    params = data.get("parameters")
    if not isinstance(params, dict) or params.get("retry_after") is None:
        return None
    try:
        value = float(params["retry_after"])
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _error_code(data: dict[str, Any]) -> int | None:
    try:
        code = int(data.get("error_code") or 0)
    except (TypeError, ValueError):
        return None
    return code or None


def offset_path_for(journal_path: str | Path) -> str:
    p = Path(journal_path)
    return str(p.with_name(p.stem + ".tg_offset"))


def _read_offset(path: str | None) -> int | None:
    if not path:
        return None
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _write_offset(path: str, offset: int) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(str(int(offset)), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(dest)


def _http_error(exc: urllib.error.HTTPError) -> TelegramError:
    status = int(getattr(exc, "code", 0) or 0) or None
    retry_after: float | None = None
    hdrs = getattr(exc, "headers", None)
    if hdrs is not None:
        raw_ra = hdrs.get("Retry-After")
        if raw_ra:
            try:
                retry_after = float(raw_ra)
            except (TypeError, ValueError):
                retry_after = None
    body: dict[str, Any] = {}
    try:
        parsed = json.loads(exc.read().decode("utf-8"))
        if isinstance(parsed, dict):
            body = parsed
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    if retry_after is None:
        retry_after = _retry_after_from(body)
    if status is None:
        status = _error_code(body)
    return TelegramError(f"telegram http {status or 'error'}", status=status, retry_after=retry_after)


@dataclass(frozen=True)
class TgCommand:
    chat_id: str
    user_id: int
    text: str
    update_id: int

    @property
    def name(self) -> str:
        if not self.text:
            return ""
        first = self.text.strip().split(maxsplit=1)[0]
        first = first.split("@", 1)[0]
        if first.startswith("/"):
            return first[1:].lower()
        return ""

    @property
    def args(self) -> str:
        parts = self.text.strip().split(maxsplit=1)
        if self.text.startswith("/"):
            return parts[1] if len(parts) > 1 else ""
        return self.text.strip()


def parse_command(update: dict[str, Any]) -> TgCommand | None:
    msg = update.get("message") or update.get("edited_message") or {}
    if not isinstance(msg, dict):
        return None
    text = msg.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    chat = msg.get("chat") or {}
    user = msg.get("from") or {}
    return TgCommand(
        chat_id=str(chat.get("id", "")),
        user_id=int(user.get("id", 0) or 0),
        text=text.strip(),
        update_id=int(update.get("update_id", 0) or 0),
    )


#: Telegram's own hard limit is 4096 characters per message; this is the slice
#: the sender cuts at, left where it has always been.
CHUNK_CHARS = 3900

#: Most sends one logical message may become. The chunk loop had no ceiling, so
#: a message that had compounded to ~100 KB became dozens of separate
#: `sendMessage` calls, which Telegram's rate limiter then dribbled out over
#: about ten minutes. That burst read as several bot instances running
#: (straightedge#119).
#:
#: Three, because this is a NOTIFICATION channel and 3 x 3900 is already far
#: past what anyone reads on a phone. Nothing legitimate comes close: the
#: longest fixed string the desk sends is HELP at 618 characters, and
#: `history_text` is bounded per row by `engine.HISTORY_ROW_CHARS`, so every
#: real message is one chunk with room to spare. Telegram allows roughly one
#: message per second to a chat, so three sends is a sub-second burst rather
#: than a backlog. A message needing a fourth chunk is not a notification any
#: more, it is a log dump, and the right answer is to see that it happened.
MAX_SEND_CHUNKS = 3


def _chunks(text: str, size: int) -> list[str]:
    if size <= 0:
        raise ValueError("chunk size must be > 0")
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


HELP = (
    "straightedge  (not financial advice)\n"
    "/quote [SYMBOL]\n"
    "/buy SYMBOL [sl=] [tp=] [limit=PRICE] [stop=PRICE]\n"
    "/sell SYMBOL [sl=] [tp=] [limit=PRICE] [stop=PRICE]\n"
    "/close TICKET|SYMBOL|all [VOL]\n"
    "/closeby TICKET OTHER\n"
    "/reverse TICKET [sl=] [tp=]\n"
    "/sl TICKET PRICE   /tp TICKET PRICE [VOL]   /be TICKET\n"
    "/trail on|off|TICKET\n"
    "/replace TICKET PRICE\n"
    "/confirm  /cancel [TICKET]  /orders  /history  /recap  /risk\n"
    "/symbols list|add|remove [SYMBOL]\n"
    "/positions  /status  /ask ...\n"
    "/model grok|claude|computer   /auto on|off\n"
    "/approve always|off   /live on I-ACCEPT-RISK|off\n"
    "/halt  /resume  /help\n"
    "Anything not a slash command goes to the AI."
)


# Safety events an operator's notify_events list cannot silence. Every config.toml
# written before these events existed enumerates notify_events explicitly, so a
# filterable alert would reach nobody on any existing install. Keep this set tiny:
# it is for residual-exposure alarms only.
ALWAYS_NOTIFY_EVENTS = frozenset({"flatten_incomplete"})


@dataclass
class TelegramClient:
    token: str
    chat_id: str
    notify_events: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "start",
                "stop",
                "open",
                "close",
                "halt",
                "order_check_fail",
                "pending",
                "recap",
                "flatten_incomplete",
            }
        )
    )
    transport: Transport = field(default_factory=UrlLibTransport)
    allow_senders: frozenset[int] = frozenset()
    audit_fn: Any = None
    offset: int = 0
    offset_path: str | None = None
    sleep_fn: Any = field(default=time.sleep)

    def __post_init__(self) -> None:
        self._acked = 0
        loaded = _read_offset(self.offset_path)
        if loaded is not None:
            self.offset = loaded
            self._acked = loaded

    @classmethod
    def from_config(
        cls,
        cfg: TelegramConfig,
        transport: Transport | None = None,
        *,
        offset_path: str | None = None,
    ) -> TelegramClient | None:
        if not cfg.enabled:
            return None
        kwargs: dict[str, Any] = {
            "token": cfg.token,
            "chat_id": str(cfg.chat_id),
            "notify_events": frozenset(cfg.notify_events),
        }
        if is_shared_chat_id(cfg.chat_id) and not cfg.allow_senders:
            raise ValueError(
                "telegram.chat_id is a shared chat: set telegram.allow_senders "
                "(or TELEGRAM_ALLOW_SENDERS) to the operator sender ids"
            )
        kwargs["allow_senders"] = frozenset(cfg.allow_senders)
        if transport is not None:
            kwargs["transport"] = transport
        if offset_path is not None:
            kwargs["offset_path"] = offset_path
        return cls(**kwargs)

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _url(self, method: str) -> str:
        return f"{API_ROOT}/bot{self.token}/{method}"

    def _store_offset(self) -> None:
        if not self.offset_path:
            return
        try:
            _write_offset(self.offset_path, int(self._acked))
        except OSError:
            return

    def ack(self, update_id: int) -> None:
        nxt = int(update_id) + 1
        if nxt > self.offset:
            self.offset = nxt
        if nxt > self._acked:
            self._acked = nxt
            self._store_offset()

    def _post(self, method: str, payload: dict[str, Any], *, timeout: float = 10.0) -> dict[str, Any]:
        last: TelegramError | None = None
        for attempt in range(RETRY_TRIES):
            try:
                data = self.transport.post_json(self._url(method), payload, timeout=timeout)
            except TelegramError as exc:
                last = exc
            else:
                if isinstance(data, dict) and data.get("ok"):
                    return data
                status = _error_code(data) if isinstance(data, dict) else None
                retry_after = _retry_after_from(data) if isinstance(data, dict) else None
                last = TelegramError("telegram api error", status=status, retry_after=retry_after)
            if last is None:
                raise TelegramError("telegram http failed")
            if attempt >= RETRY_TRIES - 1 or not retryable_status(last.status):
                raise last
            self.sleep_fn(backoff_seconds(attempt, last.retry_after))
        raise last or TelegramError("telegram http failed")

    def send(self, text: str) -> bool:
        if not self.enabled or not text:
            return False
        ok = True
        for chunk in self._outbound_chunks(text):
            try:
                data = self._post(
                    "sendMessage",
                    {
                        "chat_id": self.chat_id,
                        "text": redact_text(chunk),
                        "disable_web_page_preview": True,
                    },
                )
            except TelegramError:
                return False
            ok = ok and bool(data.get("ok"))
        return ok

    def _outbound_chunks(self, text: str) -> list[str]:
        """Chunk the message, and refuse to let one message become a burst.

        A silent truncation would be a worse bug than a long message, so it is
        reported twice: inline, where the operator is already looking, and to
        the journal through the audit seam, where it is countable after the
        fact. The notice is fitted INSIDE the last chunk rather than appended
        to it, so the result cannot cross Telegram's 4096-character limit.
        """
        chunks = _chunks(redact_text(text), CHUNK_CHARS)
        if len(chunks) <= MAX_SEND_CHUNKS:
            return chunks
        kept = chunks[:MAX_SEND_CHUNKS]
        dropped = sum(len(c) for c in chunks[MAX_SEND_CHUNKS:])
        notice = f"\n[truncated: {dropped} more chars, see the journal]"
        kept[-1] = kept[-1][: CHUNK_CHARS - len(notice)] + notice
        self._audit(
            "notify_truncated",
            {
                "sent_chunks": len(kept),
                "total_chunks": len(chunks),
                "dropped_chars": dropped,
            },
        )
        return kept

    def reject_reason(self, cmd: TgCommand) -> str | None:
        """None when the command may run; otherwise why it may not.

        A sender that cannot be determined is refused: an unmeasured
        identity is not an authorized one. With no allow_senders the chat
        id must be private, which start-up enforces, and a private chat id
        equals the one sender that can post in it.
        """
        if cmd.chat_id != str(self.chat_id):
            return "chat"
        if cmd.user_id <= 0:
            return "sender_unknown"
        if self.allow_senders and cmd.user_id not in self.allow_senders:
            return "sender"
        return None

    def authorized(self, cmd: TgCommand) -> bool:
        return self.reject_reason(cmd) is None

    def _audit_rejected(self, cmd: TgCommand, reason: str) -> None:
        fields = {
            "reason": reason,
            "user_id": cmd.user_id,
            "chat_id": cmd.chat_id,
            "command": cmd.name,
            "update_id": cmd.update_id,
        }
        self._audit("command_rejected", fields)

    def _audit(self, event: str, fields: dict[str, Any]) -> None:
        """Journal-only record. Never raises: auditing must not kill a send."""
        if self.audit_fn is None:
            print(f"{event} {fields}", file=sys.stderr)
            return
        try:
            self.audit_fn(event, fields)
        except (ValueError, RuntimeError, OSError):
            return

    def poll_commands(self, *, timeout: int = 0) -> list[TgCommand]:
        if not self.enabled:
            return []
        payload = {
            "offset": self.offset,
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        try:
            data = self._post(
                "getUpdates", payload, timeout=float(timeout) + POLL_TIMEOUT_MARGIN_S
            )
        except TelegramError:
            return []
        if not data.get("ok"):
            return []
        out: list[TgCommand] = []
        for upd in data.get("result") or []:
            if not isinstance(upd, dict):
                continue
            uid = int(upd.get("update_id", 0) or 0)
            nxt = uid + 1
            if nxt > self.offset:
                self.offset = nxt
            cmd = parse_command(upd)
            if cmd is None:
                self.ack(uid)
                continue
            reason = self.reject_reason(cmd)
            if reason is not None:
                self._audit_rejected(cmd, reason)
                self.ack(uid)
                continue
            out.append(cmd)
        return out

    def notify(self, event: str, text: str) -> bool:
        if event not in self.notify_events and event not in ALWAYS_NOTIFY_EVENTS:
            return False
        return self.send(text)
