"""Grok (xAI) and Claude (Anthropic) chat. Stdlib HTTP. Keys never logged.

Conversation turns persist next to the journal so a restart does not
wipe desk context. Bound to KEEP_TURNS messages. Secrets redacted.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from straightedge.config import AdviceConfig
from straightedge.journal import redact_text
from straightedge.telegram import Transport, UrlLibTransport

KEEP_TURNS = 40

SYSTEM = (
    "You are a risk desk, not a tipster. One account. One book. "
    "Use the snapshot, quotes, positions, orders, daily_loss room, "
    "drawdown room, and history.json. Never claim consistent profits. "
    "Do not size orders. The risk engine sizes and can refuse. You do not send. "
    "If the operator asks for a trade: name price, stop, target, and why "
    "the stop is invalidation. Hold if spread vs ATR is poor. Do not stack "
    "correlated majors the same way. Conservative means defined SL, no chase, "
    "no martingale, no averaging into a loser. Always set sl and tp on buy/sell. "
    "Limit XOR stop. Close needs ticket. "
    "If the operator asks for general portfolio or book advice: do not invent "
    "a trade. Cover allocation, correlation, unused risk room, and what not to "
    "do. JSON action must be hold unless they clearly asked to execute. "
    "End with one JSON object, no fence:\n"
    '{"action":"buy"|"sell"|"close"|"hold","symbol":"EURUSD"|null,'
    '"sl":number|null,"tp":number|null,"limit":number|null,"stop":number|null,'
    '"ticket":number|null,"summary":"one line"}'
)

_JSON_TAIL = re.compile(r"\{[^{}]*\}\s*$", re.DOTALL)


@dataclass
class Advice:
    text: str
    action: str = "hold"
    symbol: str | None = None
    sl: float | None = None
    tp: float | None = None
    limit: float | None = None
    stop: float | None = None
    ticket: int | None = None
    summary: str = ""


def parse_advice(raw: str) -> Advice:
    text = (raw or "").strip()
    match = _JSON_TAIL.search(text)
    action, symbol, sl, tp, summary = "hold", None, None, None, ""
    limit, stop, ticket = None, None, None
    body = text
    if match:
        body = text[: match.start()].strip()
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            obj = {}
        if isinstance(obj, dict):
            action = str(obj.get("action") or "hold").lower()
            if action not in {"buy", "sell", "close", "hold"}:
                action = "hold"
            sym = obj.get("symbol")
            symbol = str(sym).upper() if sym else None
            sl = _num(obj.get("sl"))
            tp = _num(obj.get("tp"))
            limit = _num(obj.get("limit"))
            stop = _num(obj.get("stop"))
            ticket = _int(obj.get("ticket"))
            summary = str(obj.get("summary") or "")
    return Advice(
        text=body or text,
        action=action,
        symbol=symbol,
        sl=sl,
        tp=tp,
        limit=limit,
        stop=stop,
        ticket=ticket,
        summary=summary,
    )


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    if n is None:
        return None
    try:
        return int(n)
    except (TypeError, ValueError):
        return None


def advice_path_for(journal_path: str | Path) -> Path:
    p = Path(journal_path)
    return p.with_name(p.stem + ".advice.json")


class Advisor:
    def __init__(
        self,
        cfg: AdviceConfig,
        transport: Transport | None = None,
        persist_path: str | Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.transport = transport or UrlLibTransport()
        self.persist_path = Path(persist_path) if persist_path else None
        self._memory: list[dict[str, str]] = []
        self.load()

    def ask(
        self,
        question: str,
        context: str,
        session: str = "",
        history: list[dict[str, Any]] | None = None,
    ) -> Advice:
        if not self.cfg.enabled:
            return Advice(
                text=(
                    "no AI key. set XAI_API_KEY, ANTHROPIC_API_KEY, or "
                    "ADVICE_URL+ADVICE_TOKEN (AI_PROVIDER=grok|claude|computer)"
                )
            )
        user = f"{context}\n\nUser: {question}"
        if self.cfg.provider == "computer":
            raw = self._computer(question, context, session, history or [])
        elif self.cfg.provider == "claude":
            raw = self._claude(user)
        else:
            raw = self._grok(user)
        advice = parse_advice(raw)
        self._remember("user", question)
        self._remember("assistant", advice.text or raw)
        return advice

    def _remember(self, role: str, content: str) -> None:
        self._memory.append({"role": role, "content": redact_text(content)})
        self._memory = self._memory[-KEEP_TURNS:]
        self.save()

    def load(self) -> None:
        path = self.persist_path
        if path is None or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        turns = raw.get("turns") if isinstance(raw, dict) else raw
        if not isinstance(turns, list):
            return
        out: list[dict[str, str]] = []
        for item in turns:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            content = redact_text(str(item.get("content") or ""))
            if content:
                out.append({"role": role, "content": content})
        self._memory = out[-KEEP_TURNS:]

    def save(self) -> None:
        path = self.persist_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"turns": self._memory}, ensure_ascii=False)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        os.chmod(path, 0o600)

    def _computer(
        self,
        question: str,
        context: str,
        session: str,
        history: list[dict[str, Any]],
    ) -> str:
        data = self.transport.post_json(
            self.cfg.computer_url,
            {
                "session": session or "default",
                "question": question,
                "context": context,
                "history": history,
                "model": self.cfg.computer_model,
            },
            timeout=120.0,
            headers={"Authorization": f"Bearer {self.cfg.computer_token}"},
        )
        if data.get("error"):
            raise RuntimeError(str(data.get("error")))
        text = str(data.get("text") or "")
        if not text:
            raise RuntimeError("computer empty")
        return text

    def _grok(self, user: str) -> str:
        messages = [{"role": "system", "content": SYSTEM}, *self._memory, {"role": "user", "content": user}]
        data = self.transport.post_json(
            self.cfg.grok_url,
            {
                "model": self.cfg.grok_model,
                "messages": messages,
                "temperature": 0.2,
            },
            timeout=60.0,
            headers={"Authorization": f"Bearer {self.cfg.grok_key}"},
        )
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("grok empty")
        return str(choices[0].get("message", {}).get("content") or "")

    def _claude(self, user: str) -> str:
        # max_tokens is 8192, not the 800 this carried before, and the reason is
        # specific to the current models rather than a preference for long answers.
        # On claude-opus-5-5 thinking is ALWAYS ON and cannot be disabled, and
        # thinking tokens count against this ceiling. At 800 a real snapshot
        # question can spend the budget before producing any text, and the block
        # loop below only collects `type == "text"`, so the desk would receive an
        # empty string and report it as advice rather than as a failure. The reply
        # itself stays short because SYSTEM asks for a short reply, not because the
        # ceiling forces it.
        body = {
            "model": self.cfg.claude_model,
            "max_tokens": 8192,
            "system": SYSTEM,
            "messages": [*self._memory, {"role": "user", "content": user}],
            # Effort is stated rather than defaulted: claude-opus-5-5 defaults to
            # `medium` where the previous generation defaulted to `high`, so an
            # unstated effort silently changes depth when the model id moves.
            "output_config": {"effort": "medium"},
        }
        # ONE credential field, TWO endpoint shapes. Routing through a Cloudflare
        # AI Gateway means the gateway authenticates the caller and supplies the
        # provider credential itself (Unified Billing), so the Anthropic key is
        # neither sent nor needed; `claude_key` then carries the Cloudflare token.
        # Pointing `claude_url` straight at api.anthropic.com keeps the original
        # BYOK behaviour for a self-hoster with their own key. The URL decides,
        # so neither operator has to set a mode flag that could disagree with it.
        if "gateway.ai.cloudflare.com" in self.cfg.claude_url:
            auth = {"cf-aig-authorization": f"Bearer {self.cfg.claude_key}"}
        else:
            auth = {"x-api-key": self.cfg.claude_key}
        data = self.transport.post_json(
            self.cfg.claude_url,
            body,
            timeout=60.0,
            headers={**auth, "anthropic-version": "2023-06-01"},
        )
        blocks = data.get("content") or []
        parts = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
        return "\n".join(parts)
