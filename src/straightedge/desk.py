"""Telegram desk: full trades and AI advice. Risk still sizes every order."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

from straightedge.currencies import normalize_model_symbol
from straightedge.inflight import new_key
from straightedge.journal import redact_text
from straightedge.llm import Advice, Advisor
from straightedge.models import OrderResult, Signal, SignalKind
from straightedge.telegram import HELP, NOT_ADVICE, TgCommand


@dataclass
class Pending:
    signal: Signal | None
    volume: float
    source: str
    expires_at: float
    close_ticket: int | None = None
    #: The idempotency key for this order, minted ONCE when it was staged.
    #:
    #: It is a property of the staged order rather than of the attempt, which is
    #: the entire point: a `/confirm` whose bridge call timed out and a second
    #: `/confirm` a minute later are two attempts at ONE order, and they carry the
    #: same key. It rides the `confirm_stage` journal record, so it also survives
    #: the desk process dying between the two.
    client_id: str = ""

    def label(self) -> str:
        if self.close_ticket is not None and self.signal is None:
            return f"close #{self.close_ticket}"
        if self.signal is None:
            return "order"
        extra = f" {self.signal.pending_kind}" if self.signal.pending_kind else ""
        if self.close_ticket is not None:
            return (
                f"reverse #{self.close_ticket} {self.signal.kind.value} "
                f"{self.signal.symbol}{extra}"
            )
        return f"{self.signal.kind.value} {self.signal.symbol}{extra}"


def pending_from_record(rec: dict) -> Pending | None:
    if not isinstance(rec, dict):
        return None
    try:
        expires_at = float(rec.get("expires_at") or 0)
        volume = float(rec.get("volume") or 0)
        source = str(rec.get("source") or "")
        raw_ticket = rec.get("close_ticket")
        close_ticket = int(raw_ticket) if raw_ticket not in (None, "") else None
        client_id = str(rec.get("client_id") or "")
        sig_raw = rec.get("signal")
        signal = None
        if isinstance(sig_raw, dict) and sig_raw.get("kind") in {"buy", "sell"}:
            signal = Signal(
                kind=SignalKind(sig_raw["kind"]),
                symbol=str(sig_raw["symbol"]),
                entry=float(sig_raw["entry"]),
                sl=float(sig_raw["sl"]),
                tp=float(sig_raw["tp"]),
                atr=float(sig_raw.get("atr") or 0),
                reason=str(sig_raw.get("reason") or ""),
                fast_ema=float(sig_raw.get("fast_ema") or 0),
                slow_ema=float(sig_raw.get("slow_ema") or 0),
                adx=float(sig_raw.get("adx") or 0),
                pending_kind=str(sig_raw.get("pending_kind") or ""),
            )
        if signal is None and close_ticket is None:
            return None
        return Pending(
            signal,
            volume,
            source,
            expires_at,
            close_ticket=close_ticket,
            client_id=client_id,
        )
    except (TypeError, ValueError, KeyError):
        return None


def parse_kv(args: str) -> tuple[str, dict[str, str], list[str]]:
    parts = args.split()
    symbol = parts[0].upper() if parts else ""
    kv: dict[str, str] = {}
    positional: list[str] = []
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            kv[k.lower()] = v
        else:
            positional.append(p)
    return symbol, kv, positional


class Desk:
    def __init__(self, engine: object, advisor: Advisor | None = None) -> None:
        self.engine = engine
        self.advisor = advisor
        self.pending: Pending | None = None
        self.approve_always = False
        self.live_expired = False

    def restore_from_journal(self, journal: object, now: float | None = None) -> None:
        """Restore the desk's chat state. Never the real-money arm.

        `live_accepted` is PER PROCESS. A journal replay must not be able to arm
        real money, so a `live_on` record is reported and then declined: the
        operator re-types `/live on I-ACCEPT-RISK` in this process or nothing is
        armed. Per UTC day was the weaker reading and it still lets a crash loop
        at 00:01 UTC hold real money armed all day with nobody watching.

        The decline is never silent: it goes to the journal as `live_not_restored`
        and `/live` says so until the operator re-arms.
        """
        last_fn = getattr(journal, "last_event", None)
        if not callable(last_fn):
            return
        rec = last_fn("confirm_stage", "confirm_cancel", "confirm_sent")
        mode = last_fn("approve_always", "approve_off")
        if isinstance(mode, dict) and mode.get("event") == "approve_always":
            self.approve_always = True
        live = last_fn("live_on", "live_off")
        if isinstance(live, dict) and live.get("event") == "live_on":
            self.live_expired = True
            self._write_confirm("live_not_restored", scope="process")
        if not isinstance(rec, dict) or rec.get("event") != "confirm_stage":
            return
        stamp = time.time() if now is None else now
        try:
            expires_at = float(rec.get("expires_at") or 0)
        except (TypeError, ValueError):
            return
        if stamp > expires_at:
            return
        pending = pending_from_record(rec)
        if pending is not None:
            self.pending = pending

    def _write_confirm(
        self, event: str, pending: Pending | None = None, **extra: object
    ) -> None:
        fields: dict = dict(extra)
        if pending is not None:
            fields.update(
                {
                    "volume": pending.volume,
                    "source": pending.source,
                    "expires_at": pending.expires_at,
                    "close_ticket": pending.close_ticket,
                    "client_id": pending.client_id,
                }
            )
            if pending.signal is not None:
                fields["signal"] = pending.signal
        emit = getattr(self.engine, "_emit", None)
        if callable(emit):
            emit(event, **fields)
            return
        journal = getattr(self.engine, "journal", None)
        write = getattr(journal, "write", None)
        if callable(write):
            write(event, **fields)

    def _journal_only(self, event: str, **fields: object) -> None:
        """Write a control decision to the journal and NOT to the chat.

        A refusal is never echoed back into the chat that asked for it. PR #40
        set that precedent for command_rejected by going to journal.write
        rather than Engine._emit, and a risk refusal has the same shape: the
        chat already has its one-line answer, so broadcasting the decision as
        an event turns one refusal into two messages.

        With no journal reachable the control still reports that it fired, on
        stderr. A control that goes silent because a file is missing cannot
        tell anyone it was exercised.
        """
        journal = getattr(self.engine, "journal", None)
        write = getattr(journal, "write", None)
        if callable(write):
            try:
                write(event, **fields)
                return
            except (OSError, ValueError, RuntimeError, TypeError):
                pass
        print(redact_text(str(event) + " " + str(fields)), file=sys.stderr)

    def _reject(
        self,
        stage: str,
        reason: str,
        *,
        source: str,
        signal: Signal | None = None,
        symbol: str = "",
        ticket: int | None = None,
        command: str = "",
    ) -> None:
        """One event name for every gate that said no, on every path.

        The auto leg already wrote reject; the desk and advice legs wrote
        nothing at all. Same event now, discriminated by source (auto,
        telegram, advice) and by stage (which gate, on which leg). reason is
        the NAMED reason, and it is the field a test asserts on, never the
        prose the chat gets back.

        This event means REFUSED. A decision that could not be taken because
        nothing could be measured gets its own event name, so a reason count
        can never treat an unmeasured outcome as a rule saying no.
        """
        fields: dict[str, object] = {
            "source": source,
            "stage": stage,
            "reason": reason,
        }
        if signal is not None:
            fields["symbol"] = signal.symbol
            fields["kind"] = signal.kind.value
            fields["rr"] = signal.rr
        elif symbol:
            fields["symbol"] = symbol
        if ticket is not None:
            fields["ticket"] = ticket
        if command:
            fields["command"] = command
        self._journal_only("reject", **fields)

    def _set_pending(self, pending: Pending) -> None:
        self.pending = pending
        self._write_confirm("confirm_stage", pending)

    def _clear_pending(self, event: str) -> None:
        pending = self.pending
        self.pending = None
        if pending is not None:
            self._write_confirm(event, pending)

    def handle(self, cmd: TgCommand) -> str:
        try:
            if not cmd.name:
                return self._ask(cmd.args, session=str(cmd.chat_id))
            fn = {
                "start": lambda: HELP,
                "help": lambda: HELP,
                "status": self.engine.status_text,
                "positions": self.engine.positions_text,
                "quote": lambda: self._quote(cmd.args),
                "risk": self.engine.risk_text,
                "trail": lambda: self._trail(cmd.args),
                "buy": lambda: self._trade(SignalKind.BUY, cmd.args, "telegram"),
                "sell": lambda: self._trade(SignalKind.SELL, cmd.args, "telegram"),
                "reverse": lambda: self._reverse(cmd.args),
                "close": lambda: self._close(cmd.args),
                "closeby": lambda: self._closeby(cmd.args),
                "sl": lambda: self._stop(cmd.args, "sl"),
                "tp": lambda: self._stop(cmd.args, "tp"),
                "be": lambda: self._be(cmd.args),
                "confirm": self._confirm,
                "approve": lambda: self._approve(cmd.args),
                "live": lambda: self._live(cmd.args),
                "cancel": lambda: self._cancel(cmd.args),
                "orders": lambda: self.engine.orders_text(),
                "replace": lambda: self._replace(cmd.args),
                "history": self._history,
                "recap": self.engine.recap_text,
                "symbols": lambda: self._symbols(cmd.args),
                "ask": lambda: self._ask(cmd.args, session=str(cmd.chat_id)),
                "model": lambda: self._model(cmd.args),
                "auto": lambda: self._auto(cmd.args),
                "halt": self._halt,
                "resume": self._resume,
            }.get(cmd.name)
            if fn is None:
                return "unknown command. /help"
            return fn()
        except (ValueError, RuntimeError) as exc:
            return redact_text(str(exc))

    def _quote(self, args: str) -> str:
        symbol = args.split()[0].upper() if args.strip() else ""
        if not symbol:
            lines = []
            for name in self.engine.cfg.symbols:
                try:
                    lines.append(self.engine.quote_text(name))
                except RuntimeError:
                    continue
            return "\n".join(lines) if lines else "usage: /quote EURUSD"
        return self.engine.quote_text(symbol)

    def _trade(self, kind: SignalKind, args: str, source: str) -> str:
        symbol, kv, pos = parse_kv(args)
        name = "buy" if kind is SignalKind.BUY else "sell"
        if not symbol:
            return f"usage: /{name} SYMBOL [sl=] [tp=] [limit=PRICE] [stop=PRICE]"
        sl = _opt_float(kv.get("sl") or (pos[0] if pos else None))
        tp = _opt_float(kv.get("tp") or (pos[1] if len(pos) > 1 else None))
        limit = _opt_float(kv.get("limit"))
        stop = _opt_float(kv.get("stop"))
        sig = self.engine.market_signal(kind, symbol, sl=sl, tp=tp, limit=limit, stop=stop)
        return self._stage(sig, source)

    def _stage(self, sig: Signal, source: str) -> str:
        now = time.time()
        if self.pending is not None and now <= self.pending.expires_at:
            self._reject("stage", "pending_exists", source=source, signal=sig)
            return f"pending {self.pending.label()}; /cancel first"
        decision = self.engine.preview(sig, manual=True)
        if not decision.allowed:
            self._reject("stage", decision.reason, source=source, signal=sig)
            return f"refused: {decision.reason}"
        ttl = int(self.engine.cfg.telegram.confirm_seconds)
        self._set_pending(
            Pending(sig, decision.volume, source, now + ttl, client_id=new_key())
        )
        extra = f" {sig.pending_kind}" if sig.pending_kind else ""
        if self.approve_always:
            return self._confirm()
        return (
            f"confirm {sig.kind.value} {sig.symbol} vol={decision.volume} "
            f"@ {sig.entry} sl={sig.sl} tp={sig.tp} rr={sig.rr:.2f} "
            f"source={source}{extra}\n/confirm within {ttl}s or /cancel"
        )

    def _stage_close(self, advice: Advice) -> str:
        now = time.time()
        if self.pending is not None and now <= self.pending.expires_at:
            self._reject(
                "stage_close",
                "pending_exists",
                source="advice",
                symbol=advice.symbol or "",
                ticket=advice.ticket,
            )
            return f"pending {self.pending.label()}; /cancel first"
        ticket = advice.ticket
        if ticket is None:
            self._reject(
                "stage_close",
                "close_needs_ticket",
                source="advice",
                symbol=advice.symbol or "",
            )
            if advice.symbol:
                return f"to flatten {advice.symbol}: /close {advice.symbol}"
            return "close needs ticket"
        pos = self.engine._pos(ticket)
        if pos is None:
            self._reject(
                "stage_close",
                "no_such_ticket",
                source="advice",
                symbol=advice.symbol or "",
                ticket=ticket,
            )
            return "no such ticket"
        ttl = int(self.engine.cfg.telegram.confirm_seconds)
        # No `client_id`: a close is already idempotent at the venue because the
        # TICKET is the key. Closing #N twice fails the second time because #N is
        # gone, which is the dedupe an OPEN has no equivalent of -- nothing on the
        # MT4 side can tell two identical `OrderSend` calls apart. This asymmetry
        # is why the ledger guards sends and not closes.
        self._set_pending(Pending(None, pos.volume, "advice", now + ttl, close_ticket=ticket))
        if self.approve_always:
            return self._confirm()
        return (
            f"confirm close #{ticket} {pos.symbol} vol={pos.volume} "
            f"source=advice\n/confirm within {ttl}s or /cancel"
        )

    def _confirm(self) -> str:
        pending = self.pending
        if pending is None:
            return "nothing to confirm"
        if time.time() > pending.expires_at:
            self._clear_pending("confirm_cancel")
            return "confirm expired"
        if getattr(self.engine, "halted", False):
            self._reject(
                "confirm",
                "halted",
                source=pending.source,
                signal=pending.signal,
                ticket=pending.close_ticket,
            )
            self._clear_pending("confirm_cancel")
            return "refused: halted"
        if pending.close_ticket is not None and pending.signal is None:
            ticket = pending.close_ticket
            reply = self.engine.close_ticket(ticket, pending.source)
            if reply.startswith("closed"):
                self._clear_pending("confirm_sent")
                return f"sent close #{ticket}"
            self._clear_pending("confirm_cancel")
            return reply
        if pending.close_ticket is not None and pending.signal is not None:
            return self._confirm_reverse(pending)
        sig = pending.signal
        if sig is None:
            self._clear_pending("confirm_cancel")
            return "nothing to confirm"
        if not sig.pending_kind:
            spec = self.engine.broker.symbol(sig.symbol)
            tick = self.engine.broker.tick(sig.symbol)
            entry = tick.ask if sig.kind is SignalKind.BUY else tick.bid
            sig = sig.reprice(entry, spec)
        decision = self.engine.preview(sig, manual=True)
        if not decision.allowed:
            self._reject(
                "confirm", decision.reason, source=pending.source, signal=sig
            )
            if decision.halt:
                self._clear_pending("confirm_cancel")
            return f"refused: {decision.reason}"
        if self._already_attempted(pending.client_id):
            # The SECOND of two independent controls, and it also owns the
            # wording. `Engine._unresolved` is the first and the one that matters
            # most, because it covers every caller including the auto leg and
            # anything added later; each is demonstrated sufficient on its own in
            # `tests/test_idempotency_guard_can_go_red.py`, which is also what
            # established that this branch is load-bearing rather than cosmetic.
            #
            # It is here at all because the engine's refusal arrives as an
            # `OrderResult` that is not ok, and the line below would report that
            # as "send failed" -- telling the operator the venue rejected the
            # order when nothing was transmitted at all. On a real-money desk
            # those are different sentences.
            return (
                f"refused: unresolved send {pending.client_id}. Nothing was "
                "transmitted this time. An earlier attempt for this order left no "
                "verdict, so it may already be on the book: check /positions and "
                "the terminal, then /cancel and re-stage if nothing moved."
            )
        result = self._submit_once(sig, decision.volume, pending)
        # `None` is a send that RAISED; an unmeasured result is a send that
        # RETURNED without a verdict. Both mean the same thing about the money,
        # so both take this path. Letting the second one fall through to the
        # branch below reported "send failed retcode=-1" -- telling the operator
        # the venue rejected an order that may be filling, which is the exact
        # substitution the comment above this method's `_already_attempted`
        # branch says must never happen. The pending is NOT cleared here: the key
        # is what makes the next `/confirm` refusable.
        if result is not None and not result.transmitted:
            # The engine declined to transmit (its own duplicate control, which
            # covers callers this branch does not). Nothing left the process, so
            # it gets the refusal's words and not a lost reply's.
            return f"refused: {result.comment}"
        if result is None or not result.measured:
            return (
                f"send unresolved: no verdict came back for {pending.client_id}. "
                "It may already be on the book. Check /positions and the terminal; "
                "this order will NOT be sent again."
            )
        if not result.ok:
            self._clear_pending("confirm_cancel")
            return (
                f"send failed ok={result.ok} retcode={result.retcode} {result.comment}"
            ).strip()
        self._clear_pending("confirm_sent")
        return (
            f"sent {sig.kind.value} {sig.symbol} vol={decision.volume} "
            f"ok={result.ok} retcode={result.retcode}"
        )

    def _already_attempted(self, client_id: str) -> bool:
        """Has this exact staged order already been put on the wire once?"""
        if not client_id:
            return False
        ledger = getattr(self.engine, "inflight", None)
        get = getattr(ledger, "get", None)
        if not callable(get):
            return False
        return get(client_id) is not None

    def _submit_once(
        self, sig: Signal, volume: float, pending: Pending
    ) -> OrderResult | None:
        """Send the staged order under its own key. `None` means NO VERDICT.

        Two things happen here that did not before.

        The key travels. `Engine.submit` refuses a key that already has an open
        attempt against it, so a second `/confirm` after a timeout cannot put the
        same order on the wire twice, whatever the operator does and whether or
        not the desk restarted in between.

        The exception is caught HERE rather than left to `Desk.handle`. It had to
        be: `BridgeTimeout` subclasses `RuntimeError`, `handle` catches
        `RuntimeError` and returns the bare message, so the operator saw
        `mt4 bridge timeout` with no statement about what it meant for the order
        and with the pending still staged and re-confirmable.

        The pending is deliberately NOT cleared on this path. Clearing it would
        lose the key, and the key is the only thing that makes the refusal
        possible; it expires on its own TTL and the ledger outlives it.
        """
        try:
            return self.engine.submit(sig, volume, pending.client_id)
        except (RuntimeError, OSError) as exc:
            self._journal_only(
                "confirm_unresolved",
                client_id=pending.client_id,
                symbol=sig.symbol,
                kind=sig.kind.value,
                volume=volume,
                request=getattr(exc, "withdrawal", "") or "unknown",
                detail=redact_text(str(exc)),
            )
            return None

    def _cancel(self, args: str = "") -> str:
        token = args.split()[0] if args.strip() else ""
        if not token:
            if self.pending is None:
                return "nothing to cancel"
            self._clear_pending("confirm_cancel")
            return "cancelled"
        if not token.isdigit():
            return "usage: /cancel [TICKET]"
        return self.engine.cancel_order(int(token))

    def _reverse(self, args: str) -> str:
        parts = args.split()
        if not parts or not parts[0].isdigit():
            return "usage: /reverse TICKET [sl=] [tp=]"
        ticket = int(parts[0])
        _, kv, _ = parse_kv(args)
        sl = _opt_float(kv.get("sl"))
        tp = _opt_float(kv.get("tp"))
        now = time.time()
        if self.pending is not None and now <= self.pending.expires_at:
            self._reject(
                "reverse", "pending_exists", source="telegram", ticket=ticket
            )
            return f"pending {self.pending.label()}; /cancel first"
        sig = self.engine.reverse_signal(ticket, sl=sl, tp=tp)
        decision = self.engine.preview(sig, manual=True, exclude_ticket=ticket)
        if not decision.allowed:
            self._reject(
                "reverse",
                decision.reason,
                source="telegram",
                signal=sig,
                ticket=ticket,
            )
            return f"refused: {decision.reason}"
        ttl = int(self.engine.cfg.telegram.confirm_seconds)
        self._set_pending(
            Pending(
                sig,
                decision.volume,
                "telegram",
                now + ttl,
                close_ticket=ticket,
                client_id=new_key(),
            )
        )
        if self.approve_always:
            return self._confirm()
        return (
            f"confirm reverse #{ticket} {sig.kind.value} {sig.symbol} vol={decision.volume} "
            f"@ {sig.entry} sl={sig.sl} tp={sig.tp} rr={sig.rr:.2f} "
            f"source=telegram\n/confirm within {ttl}s or /cancel"
        )

    def _confirm_reverse(self, pending: Pending) -> str:
        ticket = pending.close_ticket
        sig = pending.signal
        if ticket is None or sig is None:
            self._clear_pending("confirm_cancel")
            return "nothing to confirm"
        pos = self.engine._pos(ticket)
        if pos is None:
            self._reject(
                "confirm_reverse",
                "no_such_ticket",
                source=pending.source,
                signal=sig,
                ticket=ticket,
            )
            self._clear_pending("confirm_cancel")
            return "no such ticket"
        spec = self.engine.broker.symbol(sig.symbol)
        tick = self.engine.broker.tick(sig.symbol)
        entry = tick.ask if sig.kind is SignalKind.BUY else tick.bid
        sig = sig.reprice(entry, spec)
        decision = self.engine.preview(sig, manual=True, exclude_ticket=ticket)
        if not decision.allowed:
            self._reject(
                "confirm_reverse",
                decision.reason,
                source=pending.source,
                signal=sig,
                ticket=ticket,
            )
            if decision.halt:
                self._clear_pending("confirm_cancel")
            return f"refused: {decision.reason}"
        closed = self.engine.close_ticket(ticket, pending.source)
        if not closed.startswith("closed"):
            self._clear_pending("confirm_cancel")
            return closed
        decision = self.engine.preview(sig, manual=True)
        if not decision.allowed:
            # The old position IS closed and the replacement was refused, so
            # the book changed. A distinct stage is what separates this from
            # the pre-close refusal, where nothing moved.
            self._reject(
                "reverse_after_close",
                decision.reason,
                source=pending.source,
                signal=sig,
                ticket=ticket,
            )
            self._clear_pending("confirm_cancel")
            return f"closed #{ticket}; reverse refused: {decision.reason}"
        if self._already_attempted(pending.client_id):
            return (
                f"closed #{ticket}; refused: unresolved send {pending.client_id}. "
                "Nothing was transmitted this time; the replacement may already be "
                "on the book. Check /positions and the terminal."
            )
        result = self._submit_once(sig, decision.volume, pending)
        if result is not None and not result.transmitted:
            return f"closed #{ticket}; refused: {result.comment}"
        if result is None or not result.measured:
            return (
                f"closed #{ticket}; send unresolved: no verdict came back for "
                f"{pending.client_id}. The replacement may be on the book. Check "
                "/positions and the terminal; it will NOT be sent again."
            )
        if not result.ok:
            self._clear_pending("confirm_cancel")
            return (
                f"closed #{ticket}; send failed ok={result.ok} "
                f"retcode={result.retcode} {result.comment}"
            ).strip()
        self._clear_pending("confirm_sent")
        return (
            f"sent reverse #{ticket} {sig.kind.value} {sig.symbol} vol={decision.volume} "
            f"ok={result.ok} retcode={result.retcode}"
        )

    def _closeby(self, args: str) -> str:
        parts = args.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return "usage: /closeby TICKET OTHER"
        return self.engine.close_by(int(parts[0]), int(parts[1]))

    def _close(self, args: str) -> str:
        parts = args.split()
        if not parts:
            return "usage: /close TICKET|SYMBOL|all [VOL]"
        token = parts[0]
        vol = float(parts[1]) if len(parts) > 1 else None
        if token.lower() == "all":
            n = self.engine.close_all("telegram")
            return f"closed {n}"
        if token.isdigit():
            return self.engine.close_ticket(int(token), "telegram", vol)
        if vol is not None:
            return "usage: /close TICKET|SYMBOL|all [VOL]"
        n = self.engine.close_symbol(token.upper(), "telegram")
        return f"closed {n} {token.upper()}"

    def _be(self, args: str) -> str:
        token = args.split()[0] if args.strip() else ""
        if not token or not token.isdigit():
            return "usage: /be TICKET"
        return self.engine.breakeven(int(token))

    def _trail(self, args: str) -> str:
        token = args.split()[0] if args.strip() else ""
        if not token:
            return f"trail={'on' if self.engine.cfg.strategy.trail else 'off'}"
        low = token.lower()
        if low in {"on", "true"}:
            self.engine.cfg.strategy.trail = True
            return "trail on"
        if low in {"off", "false"}:
            self.engine.cfg.strategy.trail = False
            return "trail off"
        if not token.isdigit():
            return "usage: /trail on|off|TICKET"
        return self.engine.trail(int(token))

    def _history(self) -> str:
        return self.engine.history_text()

    def _symbols(self, args: str) -> str:
        parts = args.split()
        action = parts[0].lower() if parts else "list"
        name = parts[1] if len(parts) > 1 else ""
        if action == "list":
            return self.engine.symbols_text()
        if action == "add":
            if not name:
                return "usage: /symbols add SYMBOL"
            return self.engine.add_symbol(name)
        if action == "remove":
            if not name:
                return "usage: /symbols remove SYMBOL"
            return self.engine.remove_symbol(name)
        return "usage: /symbols list|add|remove [SYMBOL]"

    def _replace(self, args: str) -> str:
        parts = args.split()
        if len(parts) != 2:
            return "usage: /replace TICKET PRICE"
        return self.engine.replace_pending(int(parts[0]), float(parts[1]))

    def _stop(self, args: str, which: str) -> str:
        parts = args.split()
        if which == "sl":
            if len(parts) != 2:
                return "usage: /sl TICKET PRICE"
            return self.engine.set_sl(int(parts[0]), float(parts[1]))
        if len(parts) not in {2, 3}:
            return "usage: /tp TICKET PRICE [VOL]"
        vol = float(parts[2]) if len(parts) == 3 else None
        return self.engine.set_tp(int(parts[0]), float(parts[1]), vol)

    def _ask(self, question: str, session: str = "") -> str:
        if not question.strip():
            return "ask a question, or /buy /sell"
        if self.advisor is None:
            return "AI not configured"
        # BEFORE the provider call and before the "working..." ack, because
        # this cap exists to bound a BILL. A check after the call would cost
        # exactly what it is meant to save, and a turn is billed whether or not
        # it ends in an order, which is why this is a separate budget from
        # max_trades_per_day rather than a second reading of it.
        if self.engine.risk.advice_turns_exhausted():
            self._reject(
                "advice_turn",
                "max_advice_turns_per_day",
                source="advice",
            )
            return "refused: max_advice_turns_per_day"
        tg = getattr(self.engine, "telegram", None)
        if tg is not None and getattr(tg, "enabled", False):
            try:
                tg.send("seen. working...")
            except (ValueError, RuntimeError, OSError):
                pass
        self.engine.risk.record_advice_turn()
        advice = self.advisor.ask(
            question,
            self.engine.advice_context(),
            session=session,
            history=self.engine.advice_history(),
        )
        lines = [advice.text]
        if advice.summary:
            lines.append(advice.summary)
        staged = False
        advice_blocked = False
        if advice.action in {"buy", "sell"} and advice.symbol:
            if not self.engine.cfg.advice_allows(advice.symbol):
                # The MODEL chose this instrument, not the operator. A human
                # typing /buy on an unlisted symbol chose it themselves and is
                # not gated here.
                # `normalize_model_symbol`, never `.upper()`: a refusal that
                # renames the symbol is a refusal about a lie. On
                # `EURU\u017fD` the old form journalled and displayed
                # `EURUSD`, which is an instrument the whitelist ALLOWS, so the
                # record read as a bug in the gate rather than as a rejected
                # reply (straightedge#197).
                shown = normalize_model_symbol(advice.symbol)
                self._reject(
                    "advice_symbol",
                    "symbol_not_allowed",
                    source="advice",
                    symbol=shown,
                )
                lines.append(
                    f"not staging {advice.action} {shown}: symbol_not_allowed"
                )
                advice_blocked = True
            reason = "" if advice_blocked else self.engine.advice_circuit_reason()
            if reason:
                self._journal_only(
                    "advice_circuit_block",
                    reason=reason,
                    action=advice.action,
                    symbol=advice.symbol,
                )
                lines.append(
                    f"not staging {advice.action}: circuit {reason}; hold or close only"
                )
            elif not advice_blocked:
                staged = True
                kind = SignalKind.BUY if advice.action == "buy" else SignalKind.SELL
                try:
                    sig = self.engine.market_signal(
                        kind,
                        advice.symbol,
                        sl=advice.sl,
                        tp=advice.tp,
                        limit=advice.limit,
                        stop=advice.stop,
                    )
                    lines.append(self._stage(sig, "advice"))
                except (ValueError, RuntimeError) as exc:
                    # COULD NOT MEASURE, not REFUSED. No rule said no; the
                    # order could not be built at all, so it gets its own
                    # event name and a reason count cannot absorb it.
                    self._journal_only(
                        "advice_stage_failed",
                        measured=False,
                        action=advice.action,
                        symbol=advice.symbol,
                        error=redact_text(str(exc))[:200],
                    )
                    lines.append(f"could not stage trade: {redact_text(str(exc))}")
        elif advice.action == "close":
            staged = True
            lines.append(self._stage_close(advice))
        # Closes the turn: what the model decided, never what either side said.
        # The question and the reply stay out of the journal on purpose, so the
        # redaction surface does not grow and advice_history stays prose-free.
        self._journal_only(
            "advice_turn",
            provider=getattr(self.advisor.cfg, "provider", ""),
            session=session,
            action=advice.action,
            symbol=advice.symbol,
            sl=advice.sl,
            tp=advice.tp,
            limit=advice.limit,
            stop=advice.stop,
            ticket=advice.ticket,
            staged=staged,
            # WHY this turn held, when it held because we could not READ the
            # reply rather than because the model chose to (straightedge#185).
            # Without it these two turns are byte-identical in the journal:
            # `action=hold staged=false` for a model that held, and the same for
            # a reply that violated the schema and was forced to hold. The chat
            # carries the reason in prose and `journal.jsonl` is the surface
            # anyone reconstructing a demo week reads, which is #37 and #38.
            #
            # Empty when nothing degraded, and empty is written rather than
            # omitted: a field that appears only on failure cannot be told from
            # a desk too old to emit it, which is the partition
            # `survivor_ticket` and `history_error` exist for.
            degraded=advice.degraded,
        )
        # Unconditional, and deliberately not a branch. The alternative was to
        # skip it when the reply is not really advice ("no AI key", a provider
        # error), but a branch that decides when a disclaimer is unnecessary is
        # the thing that later drops it from a reply that needed it. One line
        # on every reply out of this method cannot be wrong in that direction.
        #
        # NOT on `_stage`, which `/buy` and `/sell` also reach: the operator
        # who typed those chose the instrument and the direction themselves,
        # nothing advised them, and a not-advice line on a trade nobody
        # proposed is noise that teaches a reader to skip the line. An
        # advice-staged order is already inside this reply and so already
        # carries it.
        return "\n".join([NOT_ADVICE, *(x for x in lines if x)])

    def _model(self, args: str) -> str:
        if self.advisor is None:
            return "AI not configured"
        name = args.strip().lower()
        if not name:
            return f"provider={self.advisor.cfg.provider}"
        if name not in {"grok", "claude", "computer"}:
            return "usage: /model grok|claude|computer"
        self.advisor.cfg.provider = name
        if not self.advisor.cfg.enabled:
            return f"switched to {name} but no key is set"
        return f"provider={name}"

    def _live_needs_flag(self) -> bool:
        cfg = getattr(self.engine, "cfg", None)
        # risk.py's send gate (:255, :275) is {"mt5", "mt4"}; this warning
        # gate was != "mt5" only, so an MT4 real account skipped straight to
        # "approve always" with no live-arm warning at all (#15 item 2). The
        # send was always still refused downstream (live_not_accepted), but
        # the desk lied about the precondition until that refusal.
        if cfg is None or getattr(cfg, "mode", "paper") not in {"mt5", "mt4"}:
            return False
        if getattr(cfg, "live_accepted", False):
            return False
        broker = getattr(self.engine, "broker", None)
        if broker is None:
            return False
        try:
            acct = broker.account()
        except Exception:
            return False
        return int(getattr(acct, "trade_mode", 0) or 0) == 2

    def _live(self, args: str) -> str:
        raw = args.strip()
        cfg = getattr(self.engine, "cfg", None)
        if raw.upper() == "ON I-ACCEPT-RISK":
            if cfg is not None:
                cfg.live_accepted = True
            self.live_expired = False
            self._write_confirm("live_on")
            return (
                "live armed. real-money sends allowed if the terminal is "
                "trade_mode=2. risk still sizes and can refuse. "
                "/live off to disarm"
            )
        if raw.lower() == "off":
            if cfg is not None:
                cfg.live_accepted = False
            self.live_expired = False
            self._write_confirm("live_off")
            return "live disarmed. real-money sends refused until /live on I-ACCEPT-RISK"
        if raw.lower() == "on":
            return "usage: /live on I-ACCEPT-RISK"
        armed = bool(cfg and getattr(cfg, "live_accepted", False))
        if not armed and self.live_expired:
            return (
                "live=off. the journal has an older /live on; arming is per "
                "process and was not restored. /live on I-ACCEPT-RISK to re-arm"
            )
        return f"live={'on' if armed else 'off'}"

    def _approve(self, args: str) -> str:
        token = args.strip().lower()
        if token in {"always", "on"}:
            cfg = getattr(self.engine, "cfg", None)
            tg_cfg = getattr(cfg, "telegram", None)
            if not getattr(tg_cfg, "allow_approve_always", True):
                self._journal_only(
                    "reject",
                    source="telegram",
                    stage="approve",
                    reason="approve_always_disabled",
                    command="approve",
                )
                return (
                    "approve always is disabled on this deployment. "
                    "risk stays sizing-only; /confirm each order"
                )
            if self._live_needs_flag():
                self._reject(
                    "approve",
                    "live_not_accepted",
                    source="telegram",
                    command="approve",
                )
                return (
                    "real-money: /live on I-ACCEPT-RISK in this chat, "
                    "then /approve always"
                )
            self.approve_always = True
            self._write_confirm("approve_always")
            return (
                "approve always. risk still sizes and can refuse. "
                "/approve off to stage again"
            )
        if token in {"off"}:
            self.approve_always = False
            self._write_confirm("approve_off")
            return "approve off. /confirm required"
        return f"approve={'always' if self.approve_always else 'off'}"

    def _auto(self, args: str) -> str:
        token = args.strip().lower()
        if token in {"on", "1", "true"}:
            tg_cfg = getattr(self.engine.cfg, "telegram", None)
            if not getattr(tg_cfg, "allow_auto", True):
                self._journal_only(
                    "reject",
                    source="telegram",
                    stage="auto",
                    reason="auto_disabled",
                    command="auto",
                )
                return (
                    "auto is disabled on this deployment. "
                    "enable telegram.allow_auto in config.toml to run unattended"
                )
            self.engine.cfg.strategy.auto = True
            # Parity with /live and /approve, which both journal. Arming the
            # autonomous trader was the only one of the three left unaudited,
            # and in a handed-over deployment it is the most consequential.
            self._write_confirm("auto_on")
            return "auto on"
        if token in {"off", "0", "false"}:
            self.engine.cfg.strategy.auto = False
            self._write_confirm("auto_off")
            return "auto off"
        return f"auto={'on' if self.engine.cfg.strategy.auto else 'off'}"

    def _halt(self) -> str:
        self._clear_pending("confirm_cancel")
        self.engine.risk.write_halt_file("telegram")
        report = self.engine.flatten("telegram")
        # The equity read must never swallow the sweep result: the operator asked
        # what happened to their positions, not what the account balance is.
        try:
            acct = self.engine.broker.account()
            self.engine._emit("halt", reason="telegram", equity=acct.equity)
        except (RuntimeError, OSError, ValueError) as exc:
            self.engine._emit("halt", reason="telegram", equity=0.0, error=str(exc))
        return f"{report.summary()} /resume clears the operator HALT file."

    def _resume(self) -> str:
        leftover = self.engine.risk.clear_operator_halt()
        if leftover:
            return f"HALT file cleared; still halted: {leftover}"
        self.engine.halted = False
        return "operator halt cleared. trading may resume."


def _opt_float(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    return float(v)
