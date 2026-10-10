"""A non-finite number never reaches the journal bytes as a number (#231).

THE GATE HAS TO READ THE BYTES, AND A PYTHON-ONLY SUITE CANNOT SEE THIS AT
ALL. `json.loads` ACCEPTS a bare `Infinity` token and hands back `inf`, so a
round-trip assertion in Python passes today and would have passed before the
fix. Measured on this Mac, pre-fix, on one written line:

    python json.loads   ACCEPTS, sl=inf
    node JSON.parse     REJECTS the line
    jq                  accepts, isinfinite TRUE, prints 1.7976931348623157e+308,
                        and that printed value compares UNEQUAL to that literal

So jq holds infinity internally and serialises a plausible finite price: a
filter that asks is told the truth, and a filter that merely outputs the field
gets a number with no provenance and no error anywhere. That is what makes it
reconciliation-affecting rather than cosmetic, and why clipping is not the fix
(clipping is what jq already does).

Pinned at the WRITER, not at one producer: `_num` authors `sl`/`tp`/`limit`/
`stop` on `advice_turn`, and `rr: Infinity` already reaches `reject` rows it
does not author, so both row types are asserted here.
"""

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from straightedge.journal import Journal

BARE_TOKENS = ("Infinity", "-Infinity", "NaN")


def _journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "journal.jsonl")


def _lines(tmp_path: Path) -> list[str]:
    return (tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().splitlines()


def test_the_byte_assertion_can_fail(tmp_path: Path) -> None:
    """CONTROL FIRST. The assertion below must be able to see the defect.

    `json.dumps` with its default `allow_nan=True` is what the writer used, and
    it emits the bare token. Without this, "no bare token in the bytes" could
    be passing because of how the row happens to be shaped rather than because
    the writer refuses to emit one.
    """
    naive = json.dumps({"sl": math.inf, "tp": -math.inf, "limit": math.nan})
    assert "Infinity" in naive and "-Infinity" in naive and "NaN" in naive, naive


@pytest.mark.parametrize(
    "event,fields",
    [
        ("advice_turn", {"provider": "grok", "sl": math.inf, "tp": -math.inf, "limit": math.nan}),
        ("reject", {"reason": "rr_below_min", "rr": math.inf}),
    ],
    ids=["advice_turn", "reject"],
)
def test_no_bare_non_finite_token_reaches_the_bytes(tmp_path: Path, event, fields) -> None:
    """Both row types, because the fix is at the writer rather than a producer."""
    _journal(tmp_path).write(event, **fields)
    raw = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    for token in BARE_TOKENS:
        assert token not in raw, f"{event} row wrote a bare {token}: {raw}"


def test_the_value_is_marked_rather_than_dropped_or_clipped(tmp_path: Path) -> None:
    """Present, typed as a string, and carrying which kind it was.

    Not absent: a row that silently omits an unusable value cannot be told
    from one where the model said nothing. Not clipped: a plausible finite
    number with no provenance is the worst available failure.
    """
    _journal(tmp_path).write("advice_turn", sl=math.inf, tp=-math.inf, limit=math.nan)
    row = json.loads(_lines(tmp_path)[0])
    assert row["sl"] == "nonfinite:inf", row
    assert row["tp"] == "nonfinite:-inf", row
    assert row["limit"] == "nonfinite:nan", row
    assert isinstance(row["sl"], str), "a marked value must not still be a number"


def test_the_row_names_which_fields_were_non_finite(tmp_path: Path) -> None:
    """One key to find them by, rather than knowing which fields were numbers."""
    _journal(tmp_path).write("advice_turn", sl=math.inf, tp=1.2345, limit=math.nan)
    row = json.loads(_lines(tmp_path)[0])
    assert sorted(row["nonfinite"]) == ["limit", "sl"], row
    assert row["tp"] == 1.2345, "a finite neighbour must be untouched"


def test_a_nested_non_finite_is_marked_too(tmp_path: Path) -> None:
    """`_jsonable` is applied per top-level field and does not descend.

    Measured pre-fix: `payload={"sl": inf, "deep": [nan]}` wrote bare
    `Infinity` and `NaN` INSIDE the nested object, so a fix confined to
    top-level fields would have closed the shapes the issue named and left
    this one open. The issue does not mention it; the repro found it.
    """
    _journal(tmp_path).write("advice_turn", payload={"sl": math.inf, "deep": [math.nan]})
    raw = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    for token in BARE_TOKENS:
        assert token not in raw, raw
    row = json.loads(_lines(tmp_path)[0])
    assert row["payload"]["sl"] == "nonfinite:inf", row
    assert row["payload"]["deep"] == ["nonfinite:nan"], row
    assert sorted(row["nonfinite"]) == ["payload.deep[0]", "payload.sl"], row


def test_an_ordinary_row_is_untouched(tmp_path: Path) -> None:
    """CONTROL. A writer that stringified every number would pass the tests above."""
    _journal(tmp_path).write("open", symbol="EURUSD", volume=0.55, price=1.10234, ok=True)
    row = json.loads(_lines(tmp_path)[0])
    assert row["volume"] == 0.55 and isinstance(row["volume"], float), row
    assert row["price"] == 1.10234, row
    assert row["ok"] is True and isinstance(row["ok"], bool), "bool must not become a number"
    assert "nonfinite" not in row, "a clean row must not carry the marker"


def test_the_unencodable_fallback_writes_a_valid_present_row() -> None:
    """The tripwire, driven directly rather than claimed as covered.

    `_dump` sets `allow_nan=False`, which is what makes "cannot reach the
    bytes" structural rather than a promise about one function. It is
    unreachable while `_mark_nonfinite` is total, so it is exercised here by
    handing `_dump` a row that sanitisation never saw. `Engine._emit` calls
    `Journal.write` unwrapped, so the requirement is that this degrades
    loudly instead of raising into the trading loop.
    """
    from straightedge.journal import _dump

    line = _dump({"ts": "T", "event": "reject", "rr": math.inf})
    for token in BARE_TOKENS:
        assert token not in line, line
    row = json.loads(line)
    assert row["event"] == "reject", row
    assert "unencodable" in row, row
    assert row["nonfinite"], row


# --- the two readers Python cannot stand in for -----------------------------


def _write_one(tmp_path: Path) -> Path:
    _journal(tmp_path).write("advice_turn", provider="grok", sl=math.inf, rr=math.nan)
    one = tmp_path / "one.jsonl"
    one.write_text(_lines(tmp_path)[0] + "\n", encoding="utf-8")
    return one


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_node_can_parse_the_line(tmp_path: Path) -> None:
    """`node JSON.parse` REJECTED the pre-fix line outright.

    This is the reader that fails loudly on the old bytes, so it is the one
    that proves the line is valid JSON rather than merely Python-readable.
    """
    one = _write_one(tmp_path)
    r = subprocess.run(
        ["node", "-e", "JSON.parse(require('fs').readFileSync(process.argv[1],'utf8'))", str(one)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"node rejected the line: {r.stderr.strip()[:200]}"


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_jq_is_not_handed_a_plausible_finite_number(tmp_path: Path) -> None:
    """The reader that produced the WRONG NUMBER silently, which is the finding.

    Pre-fix, `jq` reported `isinfinite` TRUE and printed
    `1.7976931348623157e+308`, a stop the desk never saw, with no error
    anywhere. Asserted here as: the field is not infinite to jq, and jq does
    not print a float at all.
    """
    one = _write_one(tmp_path)
    r = subprocess.run(
        ["jq", "-c", "[.sl, (.sl|type), (.sl|tostring|test(\"[0-9]e\\\\+\"))]", str(one)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    value, kind, looks_like_a_float = json.loads(r.stdout)
    assert kind == "string", f"jq sees {kind}, so it can still be read as a number: {r.stdout}"
    assert value == "nonfinite:inf", r.stdout
    assert looks_like_a_float is False, f"jq printed something float-shaped: {r.stdout}"


def test_a_caller_supplied_marker_is_merged_not_replaced(tmp_path: Path) -> None:
    """A row about what could not be measured must not drop what a caller said.

    Measured on the first version of this fix: the sanitiser assigned
    `rec["nonfinite"]` outright, so a caller already using the key had its
    entry clobbered. It bites only when a caller uses the key AND the same row
    carries a non-finite value, which is why nothing noticed: with no
    non-finite value the caller's field survives untouched.

    Asserted as MEMBERSHIP rather than order. Which entry comes first is an
    implementation detail, and pinning it would make a harmless reordering red
    a gate, which is how a gate gets disabled by the next person who hits it.
    """
    _journal(tmp_path).write(
        "advice_turn", nonfinite=["caller_said_this"], sl=math.inf
    )
    row = json.loads(_lines(tmp_path)[0])
    assert set(row["nonfinite"]) == {"caller_said_this", "sl"}, row
    assert row["sl"] == "nonfinite:inf", row


def test_a_caller_marker_survives_with_nothing_to_merge(tmp_path: Path) -> None:
    """CONTROL for the merge: the untouched path must stay untouched.

    This is the case that hid the clobber, so it is pinned: with no non-finite
    value in the row there is nothing to merge, and the caller's field must
    come through exactly as given rather than being normalised or dropped by
    the merge branch.
    """
    _journal(tmp_path).write("advice_turn", nonfinite=["only_the_caller"], sl=1.5)
    row = json.loads(_lines(tmp_path)[0])
    assert row["nonfinite"] == ["only_the_caller"], row
    assert row["sl"] == 1.5, row


def test_a_nested_non_finite_is_marked_on_a_real_row_shape(tmp_path: Path) -> None:
    """The nested case on a path that exists in production, not a synthetic one.

    `history_preflight` carries a `symbols` list of per-symbol objects, so
    `symbols[1].atr` is a real path a non-finite value can occupy. A synthetic
    `payload` proves the recursion; this proves the recursion on a shape the
    desk actually writes, which is the difference between a fixture that
    exercises the code and one that exercises the system.
    """
    _journal(tmp_path).write(
        "history_preflight",
        symbols=[
            {"symbol": "EURUSD", "atr": 0.0012},
            {"symbol": "XAUUSD", "atr": math.nan},
        ],
    )
    raw = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    for token in BARE_TOKENS:
        assert token not in raw, raw
    row = json.loads(_lines(tmp_path)[0])
    assert row["symbols"][0]["atr"] == 0.0012, "a finite neighbour must be untouched"
    assert row["symbols"][1]["atr"] == "nonfinite:nan", row
    assert row["nonfinite"] == ["symbols[1].atr"], row
