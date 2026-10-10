"""Grok (xAI) and Claude (Anthropic) chat. Stdlib HTTP. Keys never logged.

Conversation turns persist next to the journal so a restart does not
wipe desk context. Bound to KEEP_TURNS messages. Secrets redacted.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from straightedge.atomic import replace_retrying_on_share_conflict
from straightedge.config import AdviceConfig
from straightedge.currencies import may_transform_symbol, normalize_model_symbol
from straightedge.journal import redact_text
from straightedge.telegram import Transport, UrlLibTransport
from urllib.parse import urlsplit

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
    #: Why the schema gate degraded this turn, empty when it did not.
    #:
    #: Set by `Advisor.ask` from its own measurement, never parsed out of
    #: the reply, so no model can write it (straightedge#185). The desk
    #: journals it on the `advice_turn` row, which is what makes a model
    #: HOLD distinguishable from a reply we could not read.
    degraded: str = ""
    action: str = "hold"
    symbol: str | None = None
    sl: float | None = None
    tp: float | None = None
    limit: float | None = None
    stop: float | None = None
    ticket: int | None = None
    summary: str = ""


CF_GATEWAY_HOST = "gateway.ai.cloudflare.com"


def _is_cf_gateway(url: str) -> bool:
    """Is this URL a Cloudflare AI Gateway endpoint? Decided on the HOST.

    This was a substring test (`CF_GATEWAY_HOST in url`) and CodeQL flagged it
    high as `py/incomplete-url-substring-sanitization`. It misfired in both
    directions, and the dangerous direction sends the Cloudflare token to a host
    that is not Cloudflare:

        https://GATEWAY.AI.CLOUDFLARE.COM/...              -> missed (hosts are
                                                              case-insensitive, so
                                                              a valid URL took the
                                                              BYOK branch)
        https://evil.example/gateway.ai.cloudflare.com/..  -> matched on the PATH
        https://evil.example/...?x=gateway.ai.cloudflare.com -> matched on the QUERY
        https://gateway.ai.cloudflare.com.evil.example/..  -> matched a lookalike
                                                              anyone can register

    `claude_url` comes from the operator's own config and there is no remote path
    into it, so this is self-inflicted rather than attacker-reachable. It is still
    a credential leaving for the wrong host on a typo, which is worth three lines
    of stdlib.

    An operator who fronts the gateway behind their OWN hostname is deliberately
    not covered: that needs an explicit opt-in, not a looser match here.
    """
    host = (urlsplit(url).hostname or "").lower()
    return host == CF_GATEWAY_HOST or host.endswith("." + CF_GATEWAY_HOST)


#: The action vocabulary, in ONE place. `parse_advice` COERCES anything outside
#: this set to "hold"; the schema REFUSES it at generation time. Both read this
#: name so the two gates cannot drift apart, which is the whole point of pinning
#: it: a schema-valid action is only safer than a parsed one if the set is CLOSED.
ADVICE_ACTIONS = ("buy", "sell", "close", "hold")

#: The keys `parse_advice` reads out of the trailing JSON object, in the order
#: SYSTEM prints them. `text` is deliberately NOT here: it is the prose, and
#: prose is what goes BEFORE the object.
_TAIL_KEYS = ("action", "symbol", "sl", "tp", "limit", "stop", "ticket", "summary")

#: What `output_config.format` constrains the Claude reply to.
#:
#: DERIVED FROM `Advice` AND `parse_advice`, field by field, and NOT from
#: SYSTEM's prose. SYSTEM is a request; this is the contract, and where the two
#: disagree the parser is what actually runs. So: `action` is the closed set
#: above; `symbol` is a string or null because `parse_advice` does
#: `str(sym).upper() if sym else None`; the four prices are number-or-null
#: because `_num` returns `float | None`; `ticket` is integer-or-null because
#: `_int` floors through `_num`; `summary` is a string because
#: `str(obj.get("summary") or "")` is. `additionalProperties` is false and every
#: key is required, which is the "and no more" half: the parser ignores unknown
#: keys silently, and a field nobody reads is a field nobody notices is wrong.
#:
#: `text` is in the schema because structured output replaces the whole reply
#: with one JSON object. Without a prose field the operator would be shown raw
#: JSON, since `Advice.text` is what the desk renders.
#: Named separately so `_schema_violations` validates against the SAME property
#: table the request sends, rather than a second copy of it that could drift.
ADVICE_PROPERTIES: dict[str, Any] = {
    "text": {"type": "string"},
    "action": {"type": "string", "enum": list(ADVICE_ACTIONS)},
    "symbol": {"type": ["string", "null"]},
    "sl": {"type": ["number", "null"]},
    "tp": {"type": ["number", "null"]},
    "limit": {"type": ["number", "null"]},
    "stop": {"type": ["number", "null"]},
    "ticket": {"type": ["integer", "null"]},
    "summary": {"type": "string"},
}

ADVICE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": ADVICE_PROPERTIES,
        "required": ["text", *_TAIL_KEYS],
        "additionalProperties": False,
    },
}


def _is_num(v: Any) -> bool:
    # bool is an int in Python, and `True` as a price is not a price.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


#: Every way a reply can break the contract, as a CLASS rather than a sentence.
#:
#: A fixed vocabulary, which is what makes the journal's record of it bounded by
#: construction (straightedge#216). Each is paired with a field name drawn from
#: `ADVICE_PROPERTIES` or with the empty string when the offending field is one
#: the MODEL named and we therefore must not repeat.
V_UNKNOWN = "unknown_field"
V_MISSING = "missing"
V_NOT_IN_ENUM = "not_in_enum"
V_NOT_A_STRING = "not_a_string"
V_WRONG_TYPE = "wrong_type"
V_BRACE = "brace"
V_NOT_ASCII = "not_ascii"
V_NOT_A_NUMBER = "not_a_number"
V_NOT_AN_INTEGER = "not_an_integer"


def _schema_violations(obj: dict[str, Any]) -> list[tuple[str, str]]:
    """How `obj` breaks ADVICE_FORMAT, as (field, class) pairs.

    Checked on OUR side as well as the API's, because the constraint is applied
    by a server we do not run and may not reach: `claude_url` can point at a
    Cloudflare AI Gateway, and a proxy that drops an unknown body key would
    leave us believing we had a schema gate while having none. A reply that
    violates the schema is therefore evidence the constraint did not apply, and
    that is exactly when it must not be trusted.

    STRUCTURED, not prose, and that is straightedge#216. This used to return
    sentences with the model's own content interpolated into them, which was
    right for the chat and wrong for the journal: a 6000 character `action`
    produced a 6053 character reason and a 6293 byte journal row against the
    512 byte bound the suite pins, so a model could author unbounded text in
    our record. One producer, two renderings: `violation_prose` keeps the
    operator sentence that #181 added and may echo a value, because the chat
    already shows the model's own prose anyway, and `violation_classes` is
    bounded by this vocabulary and is what reaches `journal.jsonl`.

    The pair's field is `""` exactly when the offending name is the MODEL's,
    which is the one case where naming the field would reintroduce the leak.
    """
    props = ADVICE_PROPERTIES
    out: list[tuple[str, str]] = []
    for _key in sorted(set(obj) - set(props)):
        out.append(("", V_UNKNOWN))
    for key in sorted(set(props) - set(obj)):
        out.append((key, V_MISSING))
    action = obj.get("action")
    if "action" in obj and action not in ADVICE_ACTIONS:
        out.append(("action", V_NOT_IN_ENUM))
    for key in ("text", "summary"):
        if key in obj and not isinstance(obj[key], str):
            out.append((key, V_NOT_A_STRING))
    if "symbol" in obj and obj["symbol"] is not None and not isinstance(obj["symbol"], str):
        out.append(("symbol", V_WRONG_TYPE))
    # A BRACE IN `symbol` IS A VIOLATION, not something to clean up. The property
    # is typed ["string","null"], so a braced symbol is schema-VALID and would
    # otherwise pass with nothing forced and nothing said. It has to be caught
    # HERE, because the desk gates a model-chosen symbol on `cfg.advice_allows`:
    # "EUR{USD}" fails that gate loudly and "EURUSD" passes it, so repairing the
    # string converts a NAMED REFUSAL into a staged order on an instrument the
    # model never named.
    if isinstance(obj.get("symbol"), str) and ("{" in obj["symbol"] or "}" in obj["symbol"]):
        out.append(("symbol", V_BRACE))
    # A NON-ASCII `symbol` IS A VIOLATION, for the same reason and with the same
    # answer (straightedge#197). It is schema-VALID by type, exactly like the
    # brace above, and `"EURU\u017fD".upper()` is `"EURUSD"`: the transform
    # renames it into a tradeable instrument. The parser no longer performs that
    # transform, so this is not what stops the order; it is what makes the
    # structured path SAY so and hold, instead of leaving the operator to infer
    # it from a `symbol_not_allowed` refusal further down. A model that emits a
    # name outside the instrument vocabulary it was given is also evidence the
    # constraint did not apply, which is this function's whole subject.
    if isinstance(obj.get("symbol"), str) and not may_transform_symbol(obj["symbol"]):
        out.append(("symbol", V_NOT_ASCII))
    for key in ("sl", "tp", "limit", "stop"):
        if key in obj and obj[key] is not None and not _is_num(obj[key]):
            out.append((key, V_NOT_A_NUMBER))
    if "ticket" in obj and obj["ticket"] is not None and not isinstance(obj["ticket"], int):
        out.append(("ticket", V_NOT_AN_INTEGER))
    return out


def violation_classes(bad: list[tuple[str, str]]) -> list[str]:
    """The journal's rendering: bounded by the schema, never by the reply.

    `field:class`, with the model's own names collapsed into one counted token,
    so the longest possible output is a function of `ADVICE_PROPERTIES` and the
    fixed vocabulary above and NOT of anything a model sends. That is the
    property straightedge#216 needed: truncating an interpolated string would
    also work and would leave a judgement about "small enough" in the code,
    where this leaves none.
    """
    unknown = sum(1 for field, klass in bad if klass == V_UNKNOWN)
    out = [
        f"{field}:{klass}" if field else klass
        for field, klass in bad
        if klass != V_UNKNOWN
    ]
    if unknown:
        out.insert(0, V_UNKNOWN if unknown == 1 else f"{V_UNKNOWN} x{unknown}")
    return out


def violation_prose(obj: dict[str, Any], bad: list[tuple[str, str]]) -> list[str]:
    """The operator's rendering, which MAY echo the reply's own content.

    #181 put the offending value in the chat deliberately and its suite pins
    that, and the exposure straightedge#216 found is not here: the chat already
    shows the model's own prose, because `Advice.text` IS that prose. What must
    not carry unbounded model text is the durable record, and that is
    `violation_classes`.
    """
    unknown = sorted(set(obj) - set(ADVICE_PROPERTIES))
    out: list[str] = []
    for field, klass in bad:
        if klass == V_UNKNOWN:
            out.append(f"unknown field {unknown.pop(0)}" if unknown else "unknown field")
        elif klass == V_MISSING:
            out.append(f"missing field {field}")
        elif klass == V_NOT_IN_ENUM:
            out.append(f"action {obj.get('action')!r} is outside {list(ADVICE_ACTIONS)}")
        elif klass == V_NOT_A_STRING:
            out.append(f"{field} is not a string")
        elif klass == V_WRONG_TYPE:
            out.append("symbol is neither a string nor null")
        elif klass == V_BRACE:
            out.append(f"symbol {obj.get('symbol')!r} contains a brace")
        elif klass == V_NOT_ASCII:
            out.append(f"symbol {obj.get('symbol')!r} is not ASCII")
        elif klass == V_NOT_A_NUMBER:
            out.append(f"{field} is neither a number nor null")
        else:
            out.append(f"{field} is neither an integer nor null")
    return out


def structured_to_parseable(
    raw: str, *, violations: list[str] | None = None
) -> str:
    """Re-shape a schema-constrained reply into prose + trailing JSON object.

    `parse_advice` STAYS THE ONE GATE that produces an `Advice`. Structured
    output is a second gate in FRONT of it, not a replacement, so this function
    hands the parser exactly the shape it was written for rather than building
    an `Advice` on a second code path that could diverge from it.

    Three inputs, three outcomes, and the fallback is the parser:

    * a schema-valid object -> prose, then the eight tail keys re-serialised.
    * not JSON at all, or JSON that is not an advice object -> returned
      UNCHANGED, which is the pre-existing behaviour. That is the degrade the
      `grok` and `computer` providers keep permanently, and it is what happens
      if a gateway strips `output_config`.
    * a structured object that VIOLATES the schema -> action forced to `hold`,
      and the reason written into the prose. Never silently coerced: the parser
      already turns an unknown action into `hold` and says nothing, and a desk
      that cannot tell "the model held" from "we could not read the model" has
      lost the distinction `docs/CONTRACT.md` requires of every other refusal:
      its "Refusal record" row says a refusing gate writes a NAMED reason, and
      "Unmeasured is not refused" says COULD NOT MEASURE stays distinct from
      REFUSED.
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(obj, dict) or "action" not in obj:
        return raw
    bad = _schema_violations(obj)
    prose_reasons = violation_prose(obj, bad)
    if violations is not None:
        # OUT OF BAND, and that is the whole point (straightedge#185). The
        # reason has to reach the journal, and both ways it could travel
        # INSIDE the reply are defects: parsing it back out of the prose is
        # string-matching our own sentence, and adding a key to the trailing
        # JSON would be a field `parse_advice` cannot tell WE wrote, because
        # the `grok` and `computer` paths have no schema and a model could
        # put that key in its own tail. A list the caller owns cannot be
        # written by a model.
        violations.extend(violation_classes(bad))
    prose = obj.get("text")
    prose = prose if isinstance(prose, str) else ""
    tail: dict[str, Any] = {k: obj.get(k) for k in _TAIL_KEYS}
    if bad:
        tail["action"] = "hold"
        note = (
            "degraded: the reply did not match the advice schema ("
            + "; ".join(prose_reasons)
            + "); action forced to hold"
        )
        prose = f"{prose}\n\n{note}" if prose else note
    # A BRACE IN A STRING VALUE WOULD DEFEAT `_JSON_TAIL`, whose character class
    # is `[^{}]*`: the object stops matching, `parse_advice` falls back to
    # `action="hold"`, and a legitimate close is dropped with nothing said.
    #
    # THE TWO FIELDS GET OPPOSITE TREATMENT, and the asymmetry is the point.
    #
    # `summary` is a one-line LABEL the desk only displays. Nothing is traded on
    # it, so stripping braces loses no meaning and buys a parseable tail.
    #
    # `symbol` NAMES THE INSTRUMENT. Stripping braces there does not clean a
    # label, it MANUFACTURES A DIFFERENT, TRADEABLE SYMBOL: "EUR{USD}" became
    # "EURUSD", which passes the `cfg.advice_allows` gate that "EUR{USD}" fails
    # loudly, turning a named `symbol_not_allowed` refusal into a staged order on
    # an instrument the model never named. It also made this path LESS
    # conservative than the parser it gates in front of: on the same reply `grok`
    # and `computer` yield symbol=None, action=hold. So a braced symbol is a
    # violation above, which forces the hold and states the reason, and is
    # emitted as NULL here. Never repaired.
    summary = tail.get("summary")
    if isinstance(summary, str):
        tail["summary"] = summary.replace("{", "").replace("}", "")
    symbol = tail.get("symbol")
    if isinstance(symbol, str) and ("{" in symbol or "}" in symbol):
        tail["symbol"] = None
    return f"{prose}\n{json.dumps(tail)}"


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
            # Reads the PINNED vocabulary rather than a second literal. The
            # comment on ADVICE_ACTIONS claims the two gates cannot drift
            # apart; with a literal here that claim was unimplemented, and
            # widening the literal was invisible to every test.
            if action not in set(ADVICE_ACTIONS):
                action = "hold"
            sym = obj.get("symbol")
            # NOT `.upper()`. Uppercasing a non-ASCII name can rename it
            # into a real instrument (straightedge#197), and `grok` and
            # `computer` have no schema gate in front of this, so the
            # parser is where the rule has to bind for them. The string is
            # left exactly as sent, so `cfg.advice_allows` refuses it by
            # name rather than the desk skipping a `None` symbol silently.
            symbol = normalize_model_symbol(str(sym)) if sym else None
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


#: The largest magnitude a venue ticket can plausibly take. MT4 and MT5 order
#: tickets are 32 or 64 bit integers, so this is generous by orders of
#: magnitude; the point is that it is FINITE, so no ticket can contribute an
#: unbounded number of digits to a journal row (straightedge#226).
_TICKET_CEILING = 2**63


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    """A ticket, or None. Never a 309 digit integer and never a raise.

    `int(n)` on a non-finite float raises `OverflowError`, which derives from
    `ArithmeticError` and is therefore NOT in the
    `(ValueError, RuntimeError, OSError)` tuple `Desk.handle_command` and
    `Engine.poll_telegram` catch: a model emitting a 400 digit ticket was an
    uncaught exception out of the command handler. Measured, and it is the same
    family #219 found in `normalize_volume` (straightedge#226).

    `math.isfinite` is the check rather than the except clause, because a
    finite-but-enormous float still produces an integer with as many digits as
    its exponent: `1e308` gave a 309 digit ticket and a 501 byte journal row on
    its own. A ticket is a venue handle, so a value no venue could have issued
    is not a ticket; refusing it is the same rule as `_num` returning None for
    a non-number.

    THE `except OverflowError` BELOW IS UNREACHABLE WHILE THE GUARD STANDS, and
    no test pins it: a review measured dropping it with the guard kept and the
    whole suite stayed green. That is the correct state for a backstop rather
    than a gap to close with a test, because a test that could only pass by
    removing the guard first would be pinning the guard twice. It is kept for
    the case the guard is ever narrowed, and it is named here so the next reader
    does not mistake an untested line for an untested behaviour.
    """
    n = _num(v)
    if n is None or not math.isfinite(n) or abs(n) > _TICKET_CEILING:
        return None
    try:
        return int(n)
    except (TypeError, ValueError, OverflowError):
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
        #: What the schema gate found on the LAST turn, owned by this object
        #: so no model can write it. `ask` clears it before every provider
        #: call; declared here so a caller reaching a provider method
        #: directly cannot hit an unset attribute (straightedge#185).
        self._last_violations: list[str] = []
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
        # CLEARED on every turn, before the provider is called. A reason
        # that outlived its own turn would attach to the next clean one,
        # which is the stale-marker shape straightedge#119 was: the only
        # writer sets it from scratch each time rather than updating it.
        self._last_violations = []
        if self.cfg.provider == "computer":
            raw = self._computer(question, context, session, history or [])
        elif self.cfg.provider == "claude":
            raw = self._claude(user)
        else:
            raw = self._grok(user)
        advice = parse_advice(raw)
        # The schema gate's reason, attached to the Advice the desk
        # journals (straightedge#185). Carried on the object rather than
        # left in the prose, because `journal.jsonl` is the surface anyone
        # reconstructing a demo week reads and the prose never reaches it.
        advice.degraded = "; ".join(self._last_violations)
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
        # NOT a bare `os.replace` (straightedge#251). This is NOT merely a
        # dropped nicety: `save` is reached from `_remember`, which `ask` calls
        # AFTER the provider has already answered and been billed, and no call
        # site wraps it. So a refused replace discards a reply the operator has
        # already paid for and that `advice.max_turns_per_day` has already
        # counted. The retry absorbs the race; that the reply is lost at all
        # when persistence fails is a control-flow question in the advice path,
        # filed as straightedge#282 rather than changed here.
        replace_retrying_on_share_conflict(tmp, path)
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
            # SAME DECISION, SAME PREDICATE, as the claude path below (#155).
            # Routing `grok_url` at a Cloudflare AI Gateway is what gives this
            # provider a call count, a token count and a cost figure, because
            # the counter then lives OUTSIDE the process it measures. An
            # estimate computed here could not serve that: the code that
            # stopped calling the model is the same code that would stop
            # incrementing our own counter, so a day of zero spend on a config
            # that should call every bar would look identical to a healthy day.
            # That is the point of this change, not accounting.
            #
            # `_is_cf_gateway` is CALLED rather than re-spelled, so a bypass
            # shape fixed for one provider is fixed for both; the two cannot
            # drift. It parses the HOST only, so the provider-specific path
            # after the gateway id is irrelevant to the decision.
            #
            # Pointing `grok_url` straight at api.x.ai keeps the original
            # direct BYOK behaviour, so a self-hoster with their own xAI key
            # changes nothing. The URL decides; there is no mode flag that
            # could disagree with it.
            headers=(
                {"cf-aig-authorization": f"Bearer {self.cfg.grok_key}"}
                if _is_cf_gateway(self.cfg.grok_url)
                else {"Authorization": f"Bearer {self.cfg.grok_key}"}
            ),
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
            # `format` and `effort` are SIBLINGS inside output_config.
            # `format` is not a top-level parameter, and the older
            # top-level `output_format` is deprecated.
            #
            # ONLY THE CLAUDE PATH SENDS THIS TODAY, and the reason is NOT
            # that the others are incapable. This comment used to say that
            # ("`grok` and `computer` cannot constrain their output"), and for
            # `grok` it was FALSE. That made it worse than no comment:
            # `config.py` ships `provider: str = "grok"`, so this was the
            # stated justification for the DEFAULT provider having a
            # parsed-and-hoped `action` rather than a schema-guaranteed one,
            # and it documented the gap as impossible to close. Nobody
            # re-opens a question the code says has no answer.
            #
            # xAI documents structured outputs on the same OpenAI-compatible
            # endpoint `grok_url` already defaults to, as a TOP-LEVEL
            # `response_format: {"type": "json_schema", "json_schema": {...}}`,
            # and its documented subset accepts every construct ADVICE_FORMAT
            # uses: the closed `action` enum, `additionalProperties: false`,
            # and `{"type": ["string", "null"]}`, which is its own documented
            # spelling for a nullable field. The constructs are not the
            # obstacle, and the parser is not what is missing either.
            #
            # WHAT IS MISSING IS ONE LIVE VERIFICATION, and leaving it missing
            # is a decision rather than an oversight. The parameter name and
            # the dialect both differ from Anthropic's `output_config.format`,
            # so whether api.x.ai accepts THIS body cannot be settled from
            # documentation; a rejected body is a 400 on the advice path for
            # the default provider of a desk being demonstrated live. One
            # probe against the real endpoint settles it, and
            # straightedge#313 carries that probe plus the decision that
            # follows it: what a MISSING tail key should mean on a provider
            # with no server-side guarantee of completeness, where
            # `_schema_violations` would otherwise force `hold` on every
            # terse-but-valid reply.
            #
            # `computer` is a separate question and this comment does not
            # cover it. That path is our own Worker (`agent/`), so what it can
            # constrain is a question about our code, not a vendor's API.
            "output_config": {"effort": "medium", "format": ADVICE_FORMAT},
        }
        # ONE credential field, TWO endpoint shapes. Routing through a Cloudflare
        # AI Gateway means the gateway authenticates the caller and supplies the
        # provider credential itself (Unified Billing), so the Anthropic key is
        # neither sent nor needed; `claude_key` then carries the Cloudflare token.
        # Pointing `claude_url` straight at api.anthropic.com keeps the original
        # BYOK behaviour for a self-hoster with their own key. The URL decides,
        # so neither operator has to set a mode flag that could disagree with it.
        if _is_cf_gateway(self.cfg.claude_url):
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
        text = "\n".join(parts).strip()
        # AN EMPTY REPLY MUST RAISE, the way `_grok` already does for an empty
        # `choices`. Without this, `parse_advice("")` returns a well-formed
        # Advice(text="", action="hold") and `Desk._ask` renders it as advice, so
        # the operator is shown an empty answer as though the model had said
        # nothing of substance rather than told the call failed.
        #
        # TWO routes produce it and `max_tokens` only addresses one:
        #   - budget exhaustion, since thinking is always on and counts against
        #     the ceiling, which is why that ceiling is 8192 and not 800;
        #   - a safety refusal, which arrives as HTTP 200 with
        #     `stop_reason == "refusal"` and no text block at all, and which no
        #     ceiling can prevent.
        # The refusal case is named separately because "the model declined" and
        # "the answer did not fit" want different operator responses.
        stop = str(data.get("stop_reason") or "")
        if not text:
            if stop == "refusal":
                raise RuntimeError("claude refused")
            raise RuntimeError(f"claude empty (stop_reason={stop or 'unknown'})")
        return structured_to_parseable(text, violations=self._last_violations)
