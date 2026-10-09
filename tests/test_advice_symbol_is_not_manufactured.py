"""straightedge#197: a transform on a model-chosen symbol must not MANUFACTURE one.

`"EURUsD".upper()` is `"EURUSD"` when the `s` is `U+017F LATIN SMALL LETTER
LONG S`, because Unicode uppercasing maps it onto ASCII `S`. The string is a
valid `["string","null"]`, carries no brace, raises no schema violation and
survives `_JSON_TAIL`, so before this change it reached the desk as a staged
BUY on EURUSD: **the model asked for an instrument that does not exist and the
desk staged one that does.**

Same shape as the brace defect #181 fixed, one character different: a repair on
`symbol` turns a NAMED REFUSAL into an order. #181's own guard cannot see it,
and that is worth stating because the guard reads like a general safety net:
`test_a_braced_symbol_is_not_more_permissive_than_the_bare_parser` compares the
structured path against the bare parser, and BOTH transform identically here.
A comparison between two paths is blind to a defect they share.

THE RULE, which is #181's applied one layer further: a model-chosen instrument
name is either something we recognise or it is a refusal, never something we
clean up. So the pre-transform string must already be ASCII. `isascii()`
separates every member of this family from every legitimate symbol, which is
why the fix is ASCII-before-transform and NOT a codepoint blocklist: the next
case-mapping character is always one nobody enumerated.

The benign case has to keep working, and it is the reason the rule is not "no
transform at all": `eurusd` is the SAME instrument in a different case, and
that is a pure ASCII case fold.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from straightedge.config import BotConfig
from straightedge.llm import parse_advice, structured_to_parseable

#: Characters whose UPPERCASE is ASCII. Not a blocklist, a test corpus: the
#: fix is the ASCII rule, and these are the witnesses that the rule binds.
#: `ss` is the expanding one (it uppercases to two characters).
#:
#: ONE of the six reaches a symbol in the shipped whitelist, and that is stated
#: rather than left to look like six exploits: only the long s lands on
#: `EURUSD`. The others uppercase to `EURFFUSD`, `EURFIUSD`, `EURUSTD`,
#: `EURUSDI` and `EURUSSD`, which the default list does not hold, so the gate
#: already refused them for an unrelated reason. All six are transformed
#: identically by the parser, which is the shared mechanism and the thing the
#: rule has to bind, and which whitelist a given character happens to hit is a
#: property of the operator's config rather than of the defect.
MANUFACTURERS = (
    ("ſ", "EURUſD", "long s"),
    ("ﬀ", "EURﬀUSD", "ff ligature"),
    ("ﬁ", "EURﬁUSD", "fi ligature"),
    ("ﬆ", "EURUﬆD", "st ligature"),
    ("ı", "EURUSDı", "dotless i"),
    ("ß", "EURUßD", "sharp s, expands to SS"),
)


def _tail(**over) -> dict:
    base = {
        "action": "buy",
        "symbol": None,
        "sl": 1.0900,
        "tp": 1.1100,
        "limit": None,
        "stop": None,
        "ticket": None,
        "summary": "take it",
    }
    base.update(over)
    return base


def _structured(**over) -> str:
    base = {"text": "the book is flat", **_tail(**over)}
    return json.dumps(base)


def _bare(**over) -> str:
    return "prose first\n" + json.dumps(_tail(**over))


# --- 1. the gate, on its own, with no parse step at all --------------------


@pytest.mark.parametrize("_ch,symbol,name", MANUFACTURERS)
def test_the_whitelist_gate_refuses_a_symbol_that_is_not_ascii(
    _ch: str, symbol: str, name: str
) -> None:
    """`advice_allows` is reachable with no parser in front of it.

    It did `symbol.upper() in {s.upper() for s in allowed}`, so it answered
    True for a string that is not the allowed instrument. Removing the
    transform from the parser alone would not have closed that, which is why
    this is tested as its own call site.
    """
    assert BotConfig().advice_allows(symbol) is False, f"{name} passed the gate"


def test_the_gate_still_allows_the_cases_it_is_for() -> None:
    """The control. A rule that refuses everything is not a gate."""
    cfg = BotConfig()
    assert cfg.advice_allows("EURUSD") is True
    assert cfg.advice_allows("eurusd") is True, "an ASCII case fold is the same symbol"
    assert cfg.advice_allows("EuRuSd") is True
    assert cfg.advice_allows("CADJPY") is False, "not in the whitelist, correctly"
    assert cfg.advice_allows("EUR{USD}") is False, "the #181 brace, still refused"
    assert cfg.advice_allows(" EURUSD") is False, "whitespace was already refused"


# --- 2. the parser, which is the only gate the bare providers have ---------


@pytest.mark.parametrize("_ch,symbol,name", MANUFACTURERS)
def test_the_parser_does_not_uppercase_a_non_ascii_symbol_into_a_real_one(
    _ch: str, symbol: str, name: str
) -> None:
    """`grok` and `computer` have no schema gate; the parser is it.

    The symbol is left EXACTLY as the model sent it, so the whitelist gate
    refuses it by name. It is deliberately not blanked: a `None` symbol makes
    the desk skip its staging block silently, and a refusal nobody can read is
    the defect this repo keeps finding.
    """
    advice = parse_advice(_bare(symbol=symbol))
    assert advice.symbol == symbol, f"{name} was transformed by the parser"
    assert advice.symbol != "EURUSD"
    assert BotConfig().advice_allows(advice.symbol or "") is False


def test_the_parser_still_uppercases_an_ascii_symbol() -> None:
    """The control, and the reason the rule is ASCII rather than no-transform."""
    assert parse_advice(_bare(symbol="eurusd")).symbol == "EURUSD"
    assert parse_advice(_bare(symbol="EURUSD")).symbol == "EURUSD"


# --- 3. end to end, through the shipped functions, both providers ----------


@pytest.mark.parametrize("_ch,symbol,name", MANUFACTURERS)
def test_neither_path_manufactures_a_tradeable_instrument(
    _ch: str, symbol: str, name: str
) -> None:
    """The measurement from the issue, as a test, on both paths at once.

    This is also the pin on WHY #181's two-path comparison was silent: the two
    paths AGREE here, and agreed before the fix too. They have to agree and
    both be safe, which a comparison cannot assert on its own.
    """
    cfg = BotConfig()
    structured = parse_advice(structured_to_parseable(_structured(symbol=symbol)))
    bare = parse_advice(_bare(symbol=symbol))
    for advice, which in ((structured, "structured"), (bare, "bare")):
        manufactured = advice.action in {"buy", "sell"} and cfg.advice_allows(
            advice.symbol or ""
        )
        assert not manufactured, (
            f"{which} path staged {name} as a tradeable instrument: "
            f"symbol={advice.symbol!r} action={advice.action}"
        )


@pytest.mark.parametrize("_ch,symbol,name", MANUFACTURERS)
def test_the_structured_path_names_it_and_holds(
    _ch: str, symbol: str, name: str
) -> None:
    """The schema path has a reason channel, so it says what was wrong.

    Same treatment as the brace: schema-VALID by type, caught anyway, because
    the question is not "is this a string" but "can this be transformed into a
    different tradeable instrument". The action is forced to hold and the
    reason reaches the operator in the prose.
    """
    out = structured_to_parseable(_structured(symbol=symbol))
    advice = parse_advice(out)
    assert advice.action == "hold", f"{name} was not held on the structured path"
    assert "not ascii" in out.lower(), f"the reason was not stated: {out!r}"


def test_a_schema_valid_ascii_reply_is_untouched() -> None:
    """The control for the violation above."""
    out = structured_to_parseable(_structured(symbol="EURUSD"))
    advice = parse_advice(out)
    assert advice.action == "buy"
    assert advice.symbol == "EURUSD"
    assert "degraded" not in out


# --- 4. what the operator is told, which must name the REAL string ---------


def test_the_refusal_names_what_the_model_said_not_what_it_uppercases_to(
    tmp_path: Path,
) -> None:
    """A reject row that renames the symbol is a reject row about a lie.

    The desk logged `symbol=advice.symbol.upper()`, so a refusal for
    `EURUsD` would have been journalled and shown as `EURUSD`: a named
    refusal for an instrument that IS allowed, which reads as a bug in the
    whitelist rather than as a rejected reply.
    """
    from straightedge.broker.paper import PaperBroker
    from straightedge.engine import Engine
    from straightedge.synthetic import generate_bars
    from straightedge.telegram import TgCommand

    symbol = "EURUſD"
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.session.enabled = False
    cfg.advice.provider = "grok"
    cfg.advice.grok_key = "k"
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    engine = Engine(cfg, broker, halt_dir=str(tmp_path))
    engine.start()

    engine.advisor.ask = lambda *a, **kw: parse_advice(_bare(symbol=symbol))  # type: ignore[method-assign]
    reply = engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    rows = [
        r
        for r in engine.journal.tail(500)
        if r.get("event") == "reject" and r.get("reason") == "symbol_not_allowed"
    ]
    assert rows, f"no named refusal was journalled: {reply}"
    assert rows[-1]["symbol"] == symbol, (
        "the refusal renamed the symbol into the one it uppercases to: "
        + repr(rows[-1]["symbol"])
    )
    assert "EURUSD" not in reply, (
        "the operator was told about an instrument the model never named: " + reply
    )
    engine.stop()


# --- 5. the operator's own list, which the loader used to uppercase --------


def test_a_non_ascii_whitelist_entry_matches_nothing_rather_than_something(
    tmp_path: Path,
) -> None:
    """The claim in `advice_allows` has to be true of a LOADED config too.

    The loader did `[str(x).upper() for x in advice_names]`, so a non-ASCII
    entry became an ASCII one before the gate could filter it: an operator
    typo of `EURUsD` (long s) would have silently widened the whitelist to
    EURUSD, which is the same manufacture one step earlier and on the
    operator's own list. It now matches nothing, which is a visible failure
    (their symbol never trades) rather than an invisible widening.
    """
    from straightedge.config import load_config

    cfg_path = tmp_path / "c.toml"
    cfg_path.write_text(
        '[account]\nmode = "paper"\n'
        '[symbols]\nnames = ["GBPUSD"]\n'
        '[advice]\nsymbols = ["EURU\\u017fD"]\n',
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_path))
    assert cfg.advice_symbols == ["EURUſD"], (
        "the loader transformed the operator's entry: " + repr(cfg.advice_symbols)
    )
    assert cfg.advice_allows("EURUSD") is False, "the whitelist widened silently"
    assert cfg.advice_allows("EURUſD") is False, "and the entry itself is refused"


def test_an_ascii_whitelist_entry_is_still_case_folded(tmp_path: Path) -> None:
    """The control: a lowercase entry in the operator's list still works."""
    from straightedge.config import load_config

    cfg_path = tmp_path / "c.toml"
    cfg_path.write_text(
        '[account]\nmode = "paper"\n'
        '[symbols]\nnames = ["GBPUSD"]\n'
        '[advice]\nsymbols = ["eurusd"]\n',
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_path))
    assert cfg.advice_allows("EURUSD") is True
    assert cfg.advice_allows("eurusd") is True
