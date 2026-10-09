"""Structured outputs on the claude path: a second gate, never a replacement.

The entire safety claim of the advice path is SYSTEM's sentence "JSON action
must be hold unless they clearly asked to execute", and today that claim rests
on a parser reading prose. `output_config.format` constrains the Claude reply to
`llm.ADVICE_FORMAT`, whose `action` is a closed enum, so the vocabulary is shut
at generation time instead of coerced after the fact.

Four properties are pinned here, and the third is the one that matters:

1. The constraint is actually REQUESTED: the body carries the schema, with
   `format` and `effort` as siblings inside `output_config`.
2. Only `claude` gets it. `grok` and `computer` cannot constrain output, and a
   provider that cannot must not silently lose the parser.
3. `parse_advice` is still the gate. An off-schema reply is forced to `hold`
   and the reason is STATED, where today it is coerced in silence or, for an
   unknown field beside a `buy`, not caught at all.
4. The prose still reaches the operator. Structured output replaces the whole
   reply with one JSON object, so without a `text` field in the schema the desk
   would render raw JSON as advice.

WHAT THESE TESTS CANNOT SEE: the constraint itself is applied by Anthropic's
server. Everything below stubs `transport.post_json`, so these prove that the
schema is SENT and that an off-schema reply is REFUSED on our side. They cannot
prove the API honours the schema, and they cannot prove a Cloudflare AI Gateway
forwards `output_config` rather than dropping an unknown body key. That is why
`_schema_violations` validates on our side at all.
"""

import json

from straightedge.config import AdviceConfig
from straightedge.llm import (
    ADVICE_ACTIONS,
    ADVICE_FORMAT,
    Advisor,
    parse_advice,
    structured_to_parseable,
)


class FakeTransport:
    """Records every request body and replays one canned response."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.sent: list[tuple[str, dict]] = []

    def post_json(self, url: str, payload: dict, timeout: float = 10.0, headers=None) -> dict:
        self.sent.append((url, payload))
        return self.payload


def _claude_reply(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def _advisor(payload: dict, provider: str = "claude") -> tuple[Advisor, FakeTransport]:
    cfg = AdviceConfig(
        provider=provider,
        claude_key="k",
        grok_key="k",
        computer_url="https://example.invalid/ask",
        computer_token="k",
    )
    transport = FakeTransport(payload)
    return Advisor(cfg, transport=transport), transport


def _obj(**over) -> dict:
    base = {
        "text": "Spread is wide and the book is already short USD.",
        "action": "hold",
        "symbol": None,
        "sl": None,
        "tp": None,
        "limit": None,
        "stop": None,
        "ticket": None,
        "summary": "stand aside",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# 1. the constraint is requested, and the vocabulary in it is closed
# --------------------------------------------------------------------------


def test_the_claude_body_carries_the_schema_with_effort_as_a_sibling() -> None:
    advisor, transport = _advisor(_claude_reply(json.dumps(_obj())))
    advisor.ask("what is my risk?", "snapshot")

    _url, body = transport.sent[0]
    output_config = body["output_config"]
    assert output_config["format"] == ADVICE_FORMAT
    assert output_config["effort"] == "medium", "effort must stay stated, not defaulted"
    assert "format" not in body, "format is NOT a top-level parameter"
    assert "output_format" not in body, "the top-level output_format is deprecated"


def test_the_schema_closes_the_action_vocabulary() -> None:
    """A schema-valid action is only safer than a parsed one if the set is closed."""
    schema = ADVICE_FORMAT["schema"]
    assert ADVICE_FORMAT["type"] == "json_schema"
    assert schema["properties"]["action"]["enum"] == list(ADVICE_ACTIONS)
    assert set(ADVICE_ACTIONS) == {"buy", "sell", "close", "hold"}
    assert schema["additionalProperties"] is False, "additionalProperties must be closed"
    assert set(schema["required"]) == set(schema["properties"]), (
        "a field nobody reads is a field nobody notices is wrong"
    )


def test_the_schema_accepts_exactly_what_parse_advice_accepts() -> None:
    """Derived from `Advice` and `parse_advice`, not from SYSTEM's prose.

    `parse_advice` reads eight keys off the tail object and the desk renders a
    ninth thing, the prose. Anything else in the schema would be a field the
    parser ignores; anything missing would be a field the parser wants and the
    model was never asked for.
    """
    props = ADVICE_FORMAT["schema"]["properties"]
    assert set(props) == {
        "text",
        "action",
        "symbol",
        "sl",
        "tp",
        "limit",
        "stop",
        "ticket",
        "summary",
    }
    assert props["symbol"]["type"] == ["string", "null"]
    for price in ("sl", "tp", "limit", "stop"):
        assert props[price]["type"] == ["number", "null"], price
    assert props["ticket"]["type"] == ["integer", "null"]
    for text_field in ("text", "summary"):
        assert props[text_field]["type"] == "string", text_field


# --------------------------------------------------------------------------
# 2. degrade, do not break: only claude gets it
# --------------------------------------------------------------------------


def test_grok_keeps_the_parser_and_is_sent_no_schema() -> None:
    reply = 'Hold.\n{"action":"hold","symbol":null,"sl":null,"tp":null,"summary":"x"}'
    advisor, transport = _advisor({"choices": [{"message": {"content": reply}}]}, "grok")
    advice = advisor.ask("what now?", "snapshot")

    _url, body = transport.sent[0]
    assert "output_config" not in body, "grok cannot constrain output"
    assert advice.action == "hold"
    assert "Hold." in advice.text


def test_computer_keeps_the_parser_and_is_sent_no_schema() -> None:
    reply = 'Hold.\n{"action":"hold","symbol":null,"sl":null,"tp":null,"summary":"x"}'
    advisor, transport = _advisor({"text": reply}, "computer")
    advice = advisor.ask("what now?", "snapshot")

    _url, body = transport.sent[0]
    assert "output_config" not in body
    assert advice.action == "hold"


def test_a_prose_reply_on_the_claude_path_still_goes_through_the_parser() -> None:
    """The gateway-stripped-it case, and the pre-#180 shape.

    If `output_config` never reached the model, the reply is prose plus a
    trailing object, exactly as before. That must keep working: the parser is
    the fallback, so losing the constraint degrades to the old behaviour rather
    than failing the turn.
    """
    reply = 'Trend is up.\n{"action":"buy","symbol":"EURUSD","sl":1.07,"tp":1.09,"summary":"long"}'
    advisor, _transport = _advisor(_claude_reply(reply))
    advice = advisor.ask("should I buy?", "snapshot")

    assert advice.action == "buy"
    assert advice.symbol == "EURUSD"
    assert advice.text.strip() == "Trend is up."


# --------------------------------------------------------------------------
# 3. parse_advice is still the gate, and the gate now FIRES
# --------------------------------------------------------------------------


def test_an_action_outside_the_enum_is_held_and_SAID(  # noqa: N802
) -> None:
    """Today the parser coerces this to hold and says nothing.

    Silence is the defect. A desk that cannot tell "the model held" from "we
    could not read the model" loses the distinction `docs/CONTRACT.md` requires
    of every other refusal (its "Refusal record" and "Unmeasured is not refused"
    rows), so the reason is written where the operator reads it.
    """
    raw = json.dumps(_obj(action="scale_in", summary="add to the winner"))
    advice = parse_advice(structured_to_parseable(raw))

    assert advice.action == "hold"
    assert "degraded" in advice.text
    assert "scale_in" in advice.text
    assert "outside" in advice.text


def test_an_unknown_field_beside_a_buy_is_held_where_today_it_stages() -> None:
    """THE GATE FIRING. This is the un-fix case, and it is not hypothetical.

    `parse_advice` ignores unknown keys, so this exact reply parses into a
    STAGED BUY today: the control below proves that against the parser
    directly. Through the structured path the unknown field is evidence the
    schema did not apply, and a reply we cannot validate must not reach the
    desk as an order.
    """
    raw = json.dumps(
        _obj(action="buy", symbol="EURUSD", sl=1.07, tp=1.09, rationale="momentum")
    )

    # Control: the parser alone, which is what `grok` and `computer` still use.
    assert parse_advice("Buy.\n" + raw).action == "buy", (
        "if this is not buy, the test is not measuring the gate"
    )

    advice = parse_advice(structured_to_parseable(raw))
    assert advice.action == "hold"
    assert "unknown field rationale" in advice.text


def test_a_wrongly_typed_price_is_held() -> None:
    raw = json.dumps(_obj(action="sell", symbol="EURUSD", sl="not a price"))
    advice = parse_advice(structured_to_parseable(raw))
    assert advice.action == "hold"
    assert "sl is neither a number nor null" in advice.text


def test_a_missing_field_is_held() -> None:
    obj = _obj(action="close", ticket=7)
    del obj["summary"]
    advice = parse_advice(structured_to_parseable(json.dumps(obj)))
    assert advice.action == "hold"
    assert "missing field summary" in advice.text


def test_a_boolean_is_not_a_price() -> None:
    """`bool` is an `int` in Python, and True as a stop loss is not a stop loss."""
    raw = json.dumps(_obj(action="buy", symbol="EURUSD", sl=True))
    advice = parse_advice(structured_to_parseable(raw))
    assert advice.action == "hold"
    assert "sl is neither a number nor null" in advice.text


# --------------------------------------------------------------------------
# 4. a schema-valid reply still works, and the operator reads PROSE
# --------------------------------------------------------------------------


def test_a_schema_valid_reply_passes_through_with_prose_not_json() -> None:
    raw = json.dumps(
        _obj(
            text="EURUSD is trending and the book has room.",
            action="buy",
            symbol="EURUSD",
            sl=1.07,
            tp=1.09,
            summary="join the trend",
        )
    )
    advisor, _transport = _advisor(_claude_reply(raw))
    advice = advisor.ask("should I buy euro?", "snapshot")

    assert advice.action == "buy"
    assert advice.symbol == "EURUSD"
    assert advice.sl == 1.07
    assert advice.tp == 1.09
    assert advice.summary == "join the trend"
    assert advice.text == "EURUSD is trending and the book has room."
    assert "action" not in advice.text, "the operator must not be shown raw JSON"
    assert "degraded" not in advice.text


def test_a_valid_hold_carries_no_degrade_note() -> None:
    """Negative control for the note. A warning that is always on says nothing."""
    advisor, _transport = _advisor(_claude_reply(json.dumps(_obj())))
    advice = advisor.ask("general book advice?", "snapshot")

    assert advice.action == "hold"
    assert "degraded" not in advice.text
    assert advice.text == "Spread is wide and the book is already short USD."


def test_a_brace_in_the_summary_does_not_silently_drop_the_action() -> None:
    """`_JSON_TAIL` is `\\{[^{}]*\\}\\s*$`, so a brace inside a string value stops
    the object matching, and `parse_advice` then falls back to `hold` with
    nothing said: a legitimate close dropped in silence. The re-serialisation is
    the one place that can be fixed without touching the parser.
    """
    raw = json.dumps(_obj(action="close", ticket=42, summary="out of the {winner}"))
    reshaped = structured_to_parseable(raw)
    advice = parse_advice(reshaped)

    assert advice.action == "close", "a brace in a label must not cost the action"
    assert advice.ticket == 42
    assert advice.summary == "out of the winner", "braces are dropped from the label"
    # Exactly the one structural pair: the tail object itself and nothing nested,
    # which is the condition `_JSON_TAIL` can match.
    tail = reshaped.splitlines()[-1]
    assert tail.count("{") == 1 and tail.count("}") == 1, tail


def test_a_reply_that_is_not_an_advice_object_is_left_alone() -> None:
    """Bare JSON that is not advice, and plain prose, both reach the parser
    unchanged. `structured_to_parseable` must not invent an object."""
    assert structured_to_parseable("just text, no json") == "just text, no json"
    assert structured_to_parseable('{"unrelated": 1}') == '{"unrelated": 1}'
    assert structured_to_parseable("[1, 2, 3]") == "[1, 2, 3]"
    assert parse_advice(structured_to_parseable("just text, no json")).action == "hold"


# --------------------------------------------------------------------------
# 5. a symbol is NEVER repaired (the blocking finding on #181)
# --------------------------------------------------------------------------


def test_a_braced_symbol_is_held_and_never_repaired() -> None:
    """The reversal strummer found: repairing a symbol staged an order.

    `symbol` is typed `["string","null"]`, so `"EUR{USD}"` is schema-VALID and
    nothing was forced and nothing said. Stripping the braces did not clean a
    label, it MANUFACTURED a different, tradeable instrument.

    What made it a safety defect rather than a cosmetic one: the desk gates a
    model-chosen symbol on `cfg.advice_allows`. `"EUR{USD}"` fails that gate
    loudly as `symbol_not_allowed`; `"EURUSD"` passes it. So the repair
    converted a NAMED REFUSAL into a staged buy on an instrument the model
    never named, and made this path less conservative than the parser it is
    supposed to gate in front of.
    """
    raw = json.dumps(_obj(action="buy", symbol="EUR{USD}", sl=1.07, tp=1.09))
    advice = parse_advice(structured_to_parseable(raw))

    assert advice.action == "hold", "a symbol we cannot read must not stage"
    assert advice.symbol != "EURUSD", "the braces must not be repaired into a real symbol"
    assert advice.symbol is None
    assert "contains a brace" in advice.text
    assert "EUR{USD}" in advice.text, "the reason must quote what the model actually said"
    # THE VIOLATING PATH MUST BE LEGIBLE TOO (strummer's fifth mutation on #181).
    # That PR pinned "the operator must not be shown raw JSON" for the VALID
    # path only, and the violating path is where a legible reply matters most:
    # it is the one where the operator has to decide what the desk could not
    # read. Measured: dropping `tail["symbol"] = None` survived all 1238 tests,
    # because without it the braced symbol stays in the tail, `_JSON_TAIL`
    # cannot match an object with a brace inside a string, and `parse_advice`
    # falls back to treating the WHOLE reply as prose. Action and reason stay
    # correct, so it is not a safety gap; the operator just gets the raw object
    # stapled to the text and an empty summary.
    assert '"action"' not in advice.text, "the operator must not be shown raw JSON"
    assert advice.summary, "the summary must survive the violation, not come back empty"


def test_a_braced_symbol_is_not_more_permissive_than_the_bare_parser() -> None:
    """The structured path must never be the LESS conservative of the two.

    Same reply through the parser alone, which is what `grok` and `computer`
    use: the braces defeat `_JSON_TAIL`, the object does not match, and the
    action falls back to hold with no symbol. The structured path has to reach
    at least that, and it now also states why.
    """
    raw = json.dumps(_obj(action="buy", symbol="EUR{USD}", sl=1.07, tp=1.09))

    bare = parse_advice("Buy.\n" + raw)
    structured = parse_advice(structured_to_parseable(raw))

    assert bare.action == "hold" and bare.symbol is None, (bare.action, bare.symbol)
    assert structured.action == "hold" and structured.symbol is None
    assert "degraded" in structured.text and "degraded" not in bare.text


def test_a_clean_symbol_is_untouched() -> None:
    """Negative control: the brace rule must not eat ordinary symbols."""
    raw = json.dumps(_obj(action="buy", symbol="EURUSD", sl=1.07, tp=1.09))
    advice = parse_advice(structured_to_parseable(raw))
    assert advice.action == "buy"
    assert advice.symbol == "EURUSD"
    assert "degraded" not in advice.text


def test_parse_advice_reads_the_pinned_vocabulary(monkeypatch) -> None:
    """`ADVICE_ACTIONS`'s comment claims both gates read it. Now they do.

    `parse_advice` carried its own literal set, so the documented invariant was
    unimplemented and widening the literal was invisible to every test. The
    monkeypatch is what proves the name is READ at call time rather than that
    two copies happen to agree today.
    """
    import straightedge.llm as llm_mod

    assert set(llm_mod.ADVICE_ACTIONS) == {"buy", "sell", "close", "hold"}

    monkeypatch.setattr(llm_mod, "ADVICE_ACTIONS", ("buy", "sell", "close", "hold", "scale_in"))
    widened = parse_advice('x\n{"action":"scale_in","summary":"s"}')
    assert widened.action == "scale_in", (
        "parse_advice did not read ADVICE_ACTIONS; it still carries its own literal"
    )


def test_the_schema_and_the_parser_cannot_disagree_on_the_vocabulary() -> None:
    """The schema enum and the parser's accepted set are the same object."""
    import straightedge.llm as llm_mod

    assert ADVICE_FORMAT["schema"]["properties"]["action"]["enum"] == list(
        llm_mod.ADVICE_ACTIONS
    )
