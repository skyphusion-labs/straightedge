"""Every journal line is JSON that all three readers agree on (#231).

`Journal.write` ends `json.dumps(rec, default=str)`, and `allow_nan` defaults
to True, so a non-finite float was emitted as the bare token `Infinity`, which
RFC 8259 has no production for. Three readers, three answers, measured on one
line of a real journal:

    python3 json.loads   accepts, float('inf')
    node JSON.parse      REJECTS, Unexpected token 'I'
    jq-1.7.1-apple       accepts, prints 1.7976931348623157e+308

So the record did not carry an unusable value, it carried whatever the reader's
parser invented. The jq answer is the dangerous one and it is worse than a clip,
because jq's own answers disagree: `isinfinite` is true, the printed value is
`DBL_MAX`, and an equality against `DBL_MAX` is false. A reconciliation piped
through `jq` therefore reports a price the desk never saw, at a number with no
provenance, with no error anywhere. #37's evidence package is the consumer.

WHAT A PYTHON-ONLY SUITE CANNOT SEE, and why this file is shaped the way it is:
`json.loads` ACCEPTS the bad line, so a round trip through Python alone passes
today and would have passed before the fix. The gate is therefore the written
BYTES, which no reader's leniency can hide, plus a strict parse that refuses
the extension the way node does. The real `node` and `jq` are also run when
they are on the box, as corroboration and never as the gate, because a skipped
test proves nothing.

THE RULE: a non-finite number is NOT written as a number. It is written as
`null` IN PLACE, and the path is named in a `unrepresentable` list on the same
row. Three things that buys, in the order they were argued:

* VALID JSON BY CONSTRUCTION. `null` is JSON in every reader.
* NOT SILENTLY ABSENT. The marker names what was dropped and what it was, so a
  reader can tell `null` here from a field nobody set, which is the same
  discipline as `clip_for_record` saying how much it clipped (#226).
* ONE RULE, not two. `null` in place rather than omission at the top level and
  `null` when nested, because a field that can be absent OR null has two absent
  states nothing distinguishes (#225, #206).

A string spelling (`"sl": "inf"`) was the other candidate and is rejected: a
number field that is sometimes a string is the two-types-one-field shape #225
removed from a neighbouring row. Clipping to `DBL_MAX` is rejected because that
is exactly what jq already does, and a plausible finite number with no
provenance is the worst of the available failures.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from straightedge.journal import Journal

#: The bare tokens Python emits for non-finite floats, which are not JSON.
BARE_TOKENS = ("Infinity", "-Infinity", "NaN")


def _reject_constants(name: str) -> object:
    """A `parse_constant` hook that refuses the extension, the way node does."""
    raise ValueError("bare JSON constant: " + name)


def _lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "j.jsonl")


def _write_the_shapes(journal: Journal) -> None:
    """Every shape a non-finite number can arrive in, in one file.

    Flat fields, a nested dict, a list of dicts (the `history_preflight`
    shape), and a bare list. Written as separate rows so a failure names the
    shape that broke rather than only the file.
    """
    journal.write("advice_turn", symbol="EURUSD", sl=float("inf"), tp=1.2)
    journal.write("reject", reason="spread_too_wide", rr=float("inf"))
    journal.write("modify", ticket=1, sl=float("-inf"), tp=float("nan"))
    journal.write("history_preflight", symbols=[{"symbol": "EURUSD", "atr": float("nan")}])
    journal.write("depth", outer={"inner": {"value": float("inf")}})
    journal.write("series", values=[1.0, float("inf"), 3.0])


# --- 1. the gate: the bytes on disk ----------------------------------------


def test_no_row_carries_a_bare_json_constant(tmp_path: Path) -> None:
    """THE GATE. The bytes, because no reader's leniency can hide them.

    Red on `main`: the first row alone writes `"sl": Infinity`.
    """
    journal = _journal(tmp_path)
    _write_the_shapes(journal)
    text = journal.path.read_text(encoding="utf-8")
    hits = [token for token in BARE_TOKENS if token in text]
    assert not hits, (
        f"the journal contains the bare token(s) {hits!r}, which RFC 8259 has no "
        "production for: node rejects the line and jq silently reads it as "
        f"1.79e308. Offending lines: "
        f"{[ln for ln in _lines(journal.path) if any(t in ln for t in BARE_TOKENS)]!r}"
    )


def test_every_line_survives_a_parser_that_refuses_the_extension(
    tmp_path: Path,
) -> None:
    """A strict parse, which is node's verdict without needing node.

    `json.loads` accepts `Infinity` as a documented CPython extension, so the
    `parse_constant` hook is what makes a Python assertion able to see this
    defect at all.
    """
    journal = _journal(tmp_path)
    _write_the_shapes(journal)
    for n, line in enumerate(_lines(journal.path), start=1):
        try:
            json.loads(line, parse_constant=_reject_constants)
        except ValueError as exc:
            pytest.fail(f"line {n} is not RFC 8259 JSON: {exc}; line: {line[:200]}")


# --- 2. the rule: null in place, and the row says what it dropped ----------


def test_a_non_finite_number_becomes_null_and_is_named(tmp_path: Path) -> None:
    """NOT SILENTLY ABSENT. The value is `null` and the path is on the row.

    The marker is what separates this from a field nobody set, and it names
    which spelling arrived, because `inf`, `-inf` and `nan` are three different
    things to find in a record.
    """
    journal = _journal(tmp_path)
    journal.write("advice_turn", symbol="EURUSD", sl=float("inf"), tp=1.2)
    row = json.loads(_lines(journal.path)[0], parse_constant=_reject_constants)

    assert "sl" in row, "the field was dropped entirely, so a reader cannot see it existed"
    assert row["sl"] is None, f"expected null in place, found {row['sl']!r}"
    assert row["tp"] == 1.2, "a finite number beside it was altered"
    assert row["unrepresentable"] == ["sl=inf"], (
        f"the row does not name what it could not write: {row.get('unrepresentable')!r}"
    )


def test_each_spelling_is_named_as_itself(tmp_path: Path) -> None:
    """`inf`, `-inf` and `nan` are three different findings, not one."""
    journal = _journal(tmp_path)
    journal.write("modify", up=float("inf"), down=float("-inf"), undefined=float("nan"))
    row = json.loads(_lines(journal.path)[0], parse_constant=_reject_constants)
    assert row["unrepresentable"] == ["down=-inf", "undefined=nan", "up=inf"], row
    assert row["up"] is None and row["down"] is None and row["undefined"] is None, row


def test_a_nested_value_is_named_by_its_path(tmp_path: Path) -> None:
    """The `history_preflight` shape, which is where a row is already nested.

    A top-level-only fix would have passed the first test on an advice row and
    kept shipping `NaN` from the per-symbol entries, which is the one row type
    that already carries a list of dicts.
    """
    journal = _journal(tmp_path)
    journal.write(
        "history_preflight",
        symbols=[
            {"symbol": "EURUSD", "atr": 0.001},
            {"symbol": "GBPUSD", "atr": float("nan")},
        ],
    )
    row = json.loads(_lines(journal.path)[0], parse_constant=_reject_constants)
    assert row["symbols"][1]["atr"] is None, row
    assert row["symbols"][0]["atr"] == 0.001, "a finite sibling was altered"
    assert row["unrepresentable"] == ["symbols[1].atr=nan"], (
        f"the path does not locate the value: {row.get('unrepresentable')!r}"
    )


def test_a_bare_list_element_is_named_by_its_index(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.write("series", values=[1.0, float("inf"), 3.0])
    row = json.loads(_lines(journal.path)[0], parse_constant=_reject_constants)
    assert row["values"] == [1.0, None, 3.0], row
    assert row["unrepresentable"] == ["values[1]=inf"], row


def test_a_caller_supplied_marker_is_merged_and_not_replaced(tmp_path: Path) -> None:
    """The reserved field collides with a caller, and nothing is lost.

    `unrepresentable` is reserved, so a row carrying one already is either a
    caller that does not know that or a future row type that means something
    else by it. Either way the writer must not silently drop what the caller
    said in order to say its own thing: the two lists merge.

    FOUND BY MUTATION, not by design. Removing the merge left this file green,
    which made it an unpinned branch in code written for #231 itself, the same
    shape as the unreachable `except` in #226.
    """
    journal = _journal(tmp_path)
    journal.write(
        "advice_turn",
        symbol="EURUSD",
        sl=float("inf"),
        unrepresentable=["something the caller already knew"],
    )
    row = json.loads(_lines(journal.path)[0], parse_constant=_reject_constants)
    assert row["sl"] is None
    # SORTED, so the field has one order whoever wrote which entry. The point
    # of the assertion is that both survive, not which came first.
    assert row["unrepresentable"] == [
        "sl=inf",
        "something the caller already knew",
    ], f"the caller's entry was dropped or the writer's was: {row['unrepresentable']!r}"


# --- 3. the controls -------------------------------------------------------


def test_an_ordinary_row_is_untouched_and_carries_no_marker(tmp_path: Path) -> None:
    """THE CONTROL. A pass that rewrites every row would satisfy the gate.

    Without this, replacing every float with `null` would make the file
    perfectly valid JSON and perfectly useless.
    """
    journal = _journal(tmp_path)
    journal.write(
        "advice_turn",
        symbol="EURUSD",
        sl=1.0950,
        tp=1.1050,
        staged=True,
        ticket=None,
        degraded="",
        count=3,
    )
    row = json.loads(_lines(journal.path)[0], parse_constant=_reject_constants)
    assert "unrepresentable" not in row, "a clean row was marked"
    assert row["sl"] == 1.0950 and row["tp"] == 1.1050, row
    assert row["staged"] is True and row["ticket"] is None and row["count"] == 3, row
    assert row["degraded"] == "", "an empty string was altered"


def test_tail_reads_the_same_row_back(tmp_path: Path) -> None:
    """The row has to survive the reader the desk itself uses.

    `/history` and the confirm restore both read `tail()`, so a row that is
    only valid on disk is half a fix.
    """
    journal = _journal(tmp_path)
    journal.write("advice_turn", symbol="EURUSD", sl=float("inf"))
    rows = journal.tail(5)
    assert rows and rows[-1]["sl"] is None, rows
    assert rows[-1]["unrepresentable"] == ["sl=inf"], rows
    assert math.isfinite(1.0), "sanity"


# --- 4. the real readers, as corroboration and never as the gate -----------


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on this box")
def test_node_parses_every_line(tmp_path: Path) -> None:
    """The reader that REJECTS the defect, run for real.

    Corroboration only. The byte assertion above is the gate, because this
    skips on a box without node and a skipped test proves nothing.
    """
    journal = _journal(tmp_path)
    _write_the_shapes(journal)
    script = (
        "const fs=require('fs');"
        "const p=process.argv[1];"
        "for (const ln of fs.readFileSync(p,'utf8').split('\\n')) {"
        "  if (!ln.trim()) continue;"
        "  JSON.parse(ln);"
        "}"
        "console.log('ok');"
    )
    proc = subprocess.run(
        ["node", "-e", script, str(journal.path)], capture_output=True, text=True
    )
    assert proc.returncode == 0, f"node rejected a line: {proc.stderr.strip()[:300]}"
    assert proc.stdout.strip() == "ok"


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not on this box")
def test_jq_reads_no_infinity_and_invents_no_number(tmp_path: Path) -> None:
    """The reader that answered WRONG, run for real.

    This is the one that matters for #37: before the fix `jq` printed
    1.7976931348623157e+308 for a value the desk never saw, while its own
    `isinfinite` said true. Now there is nothing infinite to disagree about.
    """
    journal = _journal(tmp_path)
    _write_the_shapes(journal)
    proc = subprocess.run(
        [
            "jq",
            "-c",
            "[paths(type==\"number\" and isinfinite) , paths(type==\"number\" and isnan)]",
            str(journal.path),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"jq failed: {proc.stderr.strip()[:300]}"
    for line in proc.stdout.splitlines():
        assert json.loads(line) == [], (
            "jq still finds a non-finite number in the record, which is the "
            f"value it silently prints as 1.79e308: {line}"
        )
