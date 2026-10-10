"""Issue #232: a command that RAISES is answered in chat and never recorded.

`Desk.handle()` ends in `except (ValueError, RuntimeError): return
redact_text(str(exc))`. The operator gets a sentence and the journal gets
nothing, so the one channel that can be audited, replayed, or asserted on has
no trace that the command was even attempted.

On the advice path it also costs the operator something real. `_ask()` calls
`record_advice_turn()` BEFORE `self.advisor.ask(...)`, which is deliberate and
correct (`test_the_advice_cap_refuses_before_the_provider_is_billed` pins it,
because a turn is billed whether or not it ends in an order). The defect is
that when `ask` then raises, the turn has been **spent against the daily cap
with no record it was ever attempted**. The operator watches their advice
budget shrink and the journal cannot tell them why.

WHY NO EXISTING TEST COULD SEE THIS, which is the whole lesson. Every fake
transport in this suite RETURNS A PAYLOAD, and a payload cannot raise an HTTP
502. The defect needs the ERROR CLASS, not a value, so a suite of stubs proves
the decision path and never the shipped artifact. It was found by a live run
against a real Worker under `wrangler dev`, and it is reproduced here by a
transport that raises rather than one that answers.

THE PARTITION THIS FOLLOWS, taken from the module rather than invented.
`_reject`'s docstring: `reject` means REFUSED, and "a decision that could not
be taken because nothing could be measured gets its own event name, so a
reason count can never treat an unmeasured outcome as a rule saying no." The
desk already does exactly this one branch over, at `advice_stage_failed`
(`measured=False`, `error=redact_text(str(exc))[:200]`). A raised command is
the same third thing: no rule said no, the command could not be completed at
all. So it gets its own event and stays out of the reason counts.
"""

import socket

from test_refusal_journal import FakeLlm, _engine

from straightedge.config import AdviceConfig
from straightedge.llm import Advisor
from straightedge.telegram import TgCommand

ASK = "/ask what should I do"

# Shaped to match _XAI_KEY_RE in journal.py (xai- plus 16+ url-safe chars).
# Not a real key; the point is that a provider error echoing one must not
# persist it.
FAKE_KEY = "xai-0123456789abcdefghij"


class RaisingLlm:
    """A transport that FAILS rather than one that answers.

    This is the seam the stubs could not reach. `FakeLlm` returns a dict, so
    every test built on it exercises the parse-and-decide path and none of them
    can produce the error class the defect lives in.
    """

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def post_json(self, url, payload, timeout=10.0, headers=None) -> dict:
        del url, payload, timeout, headers
        self.calls += 1
        raise self.exc


def _one(engine, event: str) -> dict:
    rec = engine.journal.last_event(event)
    assert rec is not None, f"no structured {event!r} record was written"
    return rec


# -- the REAL transport, failing for real ---------------------------------
#
# WHAT THE REAL TRANSPORT ACTUALLY DOES WHEN IT FAILS, enumerated from
# `UrlLibTransport.post_json` rather than guessed, because the question that
# matters for a double is not "is it realistic" but "can it ENTER this state":
#
#   1. HTTP non-2xx      -> `_http_error(HTTPError)` -> TelegramError(
#                           f"telegram http {status}", status=, retry_after=)
#   2. URLError          -> TelegramError("telegram http failed")
#      (connection refused, DNS failure, timeout)
#   3. body is not JSON  -> TelegramError("telegram non-json")
#   4. provider error    -> RuntimeError(str(data["error"]))      [llm.py]
#   5. empty reply       -> RuntimeError("computer empty")        [llm.py]
#
# `RaisingLlm` below can produce 4 and 5 exactly, because those ARE bare
# RuntimeErrors raised in `llm.py`. It can only APPROXIMATE 1, 2 and 3: the
# real class is `TelegramError`, a RuntimeError SUBCLASS carrying `status` and
# `retry_after`, and a fake raising bare RuntimeError proves the handler
# journals on a raise without proving it is reachable by the thing that
# actually raises.
#
# So this case uses no double at all. It points the REAL `UrlLibTransport` at
# a REAL closed port, which produces a REAL `URLError` inside real urllib and
# a real `TelegramError` from the real `_http_error` path (mode 2). Offline and
# deterministic: a refused connection to a closed loopback port is immediate
# and needs no network.


def _closed_port() -> int:
    """A port nothing is listening on, obtained by binding and releasing it."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_the_real_transport_failing_is_journalled(tmp_path) -> None:
    """The un-stubbable seam: real transport, real exception, real handler.

    This is the case that answers "a stub cannot produce the failure mode of
    the thing it stands in for". Every other case here hands the desk an
    exception; this one makes the shipped code raise its own.
    """
    port = _closed_port()
    advisor = Advisor(
        AdviceConfig(
            provider="computer",
            computer_url=f"http://127.0.0.1:{port}/ask",
            computer_token="not-a-real-token",
        )
    )
    engine = _engine(tmp_path)
    engine.desk.advisor = advisor
    engine.advisor = advisor
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, ASK, 1))

    # The real class, not an approximation of it.
    rec = _one(engine, "command_failed")
    assert rec["measured"] is False
    assert rec["command"] == "ask"
    assert "telegram http" in rec["error"], rec["error"]

    turn = _one(engine, "advice_turn_failed")
    assert turn["turn_spent"] is True
    assert "telegram http" in turn["error"], turn["error"]

    # And the operator still got their sentence.
    assert "telegram http" in reply, reply
    engine.stop()


# -- the general case: any raised command ---------------------------------


def test_a_failed_command_leaves_a_structured_record(tmp_path) -> None:
    """`/buy` with both limit= and stop= raises, and must not vanish.

    Chosen because it raises `RuntimeError` deterministically inside the engine
    with no transport involved, so this case is about `handle()` and nothing
    else.
    """
    engine = _engine(tmp_path)
    engine.start()
    reply = engine.handle_command(
        TgCommand("1", 1, "/buy EURUSD limit=1.1 stop=1.2", 1)
    )
    assert "limit" in reply  # the operator still gets their sentence

    rec = _one(engine, "command_failed")
    assert rec["command"] == "buy"
    assert rec["measured"] is False
    assert "not both" in rec["error"]
    engine.stop()


def test_a_successful_command_writes_no_failure_record(tmp_path) -> None:
    """Negative control. A record written on every command proves nothing."""
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/risk", 1))
    assert engine.journal.last_event("command_failed") is None
    engine.stop()


def test_the_reply_the_operator_sees_is_unchanged(tmp_path) -> None:
    """The fix adds a record; it must not change what the chat says.

    The reply is the operator's only immediate signal and it is already
    redacted. A fix that improved the audit trail while altering the sentence
    would be trading one channel for another.
    """
    engine = _engine(tmp_path)
    engine.start()
    reply = engine.handle_command(
        TgCommand("1", 1, "/buy EURUSD limit=1.1 stop=1.2", 1)
    )
    assert reply == "use limit= or stop=, not both"
    engine.stop()


def test_a_provider_key_in_the_error_does_not_persist(tmp_path) -> None:
    """A provider error can echo the key that was sent to it.

    READ THIS BEFORE TRUSTING THIS TEST. It does NOT discriminate the
    `redact_text(...)` call in `handle()`. I found that by mutation: deleting
    that call leaves this case GREEN, because `Journal.write` already runs
    `redact(rec)` over every record and `redact()` applies `redact_text` to
    every string value it finds. The guarantee is the JOURNAL's, not the
    call site's.

    So what this case is actually worth: it pins that the error path writes
    through `Journal.write` at all, rather than to stderr, a second sink, or
    a pre-redaction buffer. That is a real property and a reachable
    regression. It is simply not the property the name first suggests, and a
    test whose discrimination is assumed rather than measured is the exact
    defect `docs/TESTING.md` is about.

    The `redact_text` call stays in `handle()` for consistency with
    `advice_stage_failed` and because the `[:200]` bound needs it anyway, but
    it is belt and braces over the journal, not the only barrier. The bound
    is the half this file can genuinely prove, in the case below.
    """
    llm = RaisingLlm(RuntimeError(f"provider rejected key {FAKE_KEY}"))
    engine = _engine(tmp_path, llm=llm)
    engine.start()
    engine.handle_command(TgCommand("1", 1, ASK, 1))

    rec = _one(engine, "command_failed")
    assert FAKE_KEY not in rec["error"], "a provider key was persisted"
    assert "[REDACTED]" in rec["error"]
    engine.stop()


def test_a_long_error_is_bounded_in_the_record(tmp_path) -> None:
    """The half the call site DOES own: the 200-character bound.

    Nothing in `Journal.write` truncates, so unlike the redaction above this
    is genuinely provided by `error=...[:200]` and a mutation removing the
    slice reds this case. A broker or provider can return a very long error
    body, and an append-only audit log that inherits it unbounded is a disk
    problem rather than a record.
    """
    long_error = "E" * 5000
    llm = RaisingLlm(RuntimeError(long_error))
    engine = _engine(tmp_path, llm=llm)
    engine.start()
    engine.handle_command(TgCommand("1", 1, ASK, 1))

    rec = _one(engine, "command_failed")
    assert len(rec["error"]) == 200, len(rec["error"])
    turn = _one(engine, "advice_turn_failed")
    assert len(turn["error"]) == 200, len(turn["error"])
    engine.stop()


# -- the advice path: the spent turn must be auditable --------------------


def test_a_failed_advice_turn_records_that_the_turn_was_spent(tmp_path) -> None:
    """The money-adjacent half. The cap was charged; say so.

    Without this record the operator's budget drops with nothing to point at,
    which is the defect #232 was filed for.
    """
    llm = RaisingLlm(RuntimeError("telegram http 502"))
    engine = _engine(tmp_path, llm=llm)
    engine.start()
    engine.handle_command(TgCommand("1", 1, ASK, 1))

    rec = _one(engine, "advice_turn_failed")
    assert rec["turn_spent"] is True
    assert rec["measured"] is False
    assert "502" in rec["error"]
    assert llm.calls == 1, "the provider was not actually reached"
    engine.stop()


def test_the_failed_turn_is_still_charged_against_the_cap(tmp_path) -> None:
    """Pinning what this fix does NOT change.

    Refunding the turn on failure would be the obvious reading of "the
    operator paid for nothing", and it is the wrong one: we cannot know from
    here whether the provider billed before failing, and a refund on error is
    an unbounded retry against a paid endpoint. So the turn stays spent and the
    fix makes it VISIBLE instead. This test exists so a later change cannot
    quietly turn the cap into a refundable one.
    """
    llm = RaisingLlm(RuntimeError("telegram http 502"))
    engine = _engine(tmp_path, llm=llm)
    engine.cfg.advice.max_turns_per_day = 1
    engine.start()
    engine.handle_command(TgCommand("1", 1, ASK, 1))
    assert llm.calls == 1

    # The cap is exhausted by the FAILED turn, so the second never calls out.
    engine.handle_command(TgCommand("1", 1, ASK, 2))
    assert llm.calls == 1, "a failed turn was refunded and the provider re-called"
    rec = engine.journal.last_event("reject")
    assert rec is not None and rec["reason"] == "max_advice_turns_per_day"
    engine.stop()


def test_a_successful_advice_turn_writes_no_failure_record(tmp_path) -> None:
    """Negative control for the advice half."""
    engine = _engine(tmp_path, llm=FakeLlm({"choices": [{"message": {"content": "{}"}}]}))
    engine.start()
    engine.handle_command(TgCommand("1", 1, ASK, 1))
    assert engine.journal.last_event("advice_turn_failed") is None
    engine.stop()
