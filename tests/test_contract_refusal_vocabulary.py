"""Every refusal reason the desk can emit is documented in the contract (#220).

`refused: <reason>` reaches the operator verbatim. An operator who gets a word
back and cannot look it up anywhere has a record that is not reproducible from
the docs, which is the standard `CLAUDE.md` sets for `docs/CONTRACT.md`.

MEASURED WHEN FILED: 28 reason words in the source, 15 documented, **13 not**,
including `sl_required` and `sl_not_measured` while the refusals on the very
same contract rows were documented at length. So this was an accumulated gap
rather than one PR's miss, which is why it is closed by a scan with a
denominator rather than by inserting the two words somebody happened to notice.

THE SCANNERS ARE THE PROJECT'S OWN. `tests/refusal_scan.py` already read
`risk.py` and `engine.py` for the per-reason roster, and reusing it keeps ONE
authority: a second extractor here, with its own regexes, would be the third
opinion that #187 and #208 exist to prevent, and it would drift from the roster
silently. Two narrow scanners were added there for the sources the roster never
needed (`sizing.py`'s authority functions, and words written straight after
`refused: `), in that file's own style and with its unresolved-fails rule.
"""

from __future__ import annotations

import pathlib
import re

from refusal_scan import (
    scan_decision_reasons,
    scan_reason_authorities,
    scan_reasons,
    scan_refusal_literals,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "straightedge"
CONTRACT = ROOT / "docs" / "CONTRACT.md"

#: `ok` is the ALLOWED verdict. It is carried in the same field and is never
#: rendered after `refused: `, so it is not vocabulary.
NOT_A_REFUSAL = frozenset({"ok"})

#: The one refusal that is operator PROSE rather than a word, pinned exactly so
#: a NEW prose refusal fails here and gets looked at by a person. It names a
#: specific in-flight send, so it can never be a contract entry.
PINNED_PROSE = ("prose: refused: unresolved send",)


def _scans() -> dict[str, object]:
    """Every source of a word the operator can see after `refused: `.

    Named per source rather than merged, so a failure says WHERE the word came
    from instead of only that one is missing.
    """
    return {
        "risk.py (RiskManager)": scan_reasons((SRC / "risk.py").read_text(encoding="utf-8")),
        "engine.py (RiskDecision)": scan_decision_reasons(
            (SRC / "engine.py").read_text(encoding="utf-8")
        ),
        "sizing.py (authorities)": scan_reason_authorities(
            (SRC / "sizing.py").read_text(encoding="utf-8")
        ),
        "desk.py (literals)": scan_refusal_literals((SRC / "desk.py").read_text(encoding="utf-8")),
        "engine.py (literals)": scan_refusal_literals(
            (SRC / "engine.py").read_text(encoding="utf-8")
        ),
    }


def reason_words() -> dict[str, str]:
    """Word -> the source that names it. The denominator this test is built on."""
    out: dict[str, str] = {}
    for where, scan in _scans().items():
        for name in scan.names:
            if name not in NOT_A_REFUSAL:
                out.setdefault(name, where)
    return out


#: Heading of the section whose table IS the enumeration.
TABLE_HEADING = "### Refusal reasons"

#: A table row naming one reason: `| `word` |` or `| `word:` |` for a prefix.
TABLE_ROW = re.compile(r"^\| `([a-z][a-z0-9_]*):?` \|", re.M)


def table_words(contract_text: str) -> frozenset[str]:
    """The words the refusal-reason TABLE documents, trailing `:` stripped.

    ANCHORED TO THE TABLE, NOT TO THE FILE, and that distinction is the whole
    gate. An earlier version asked `word not in contract_text`, which passes on
    any mention anywhere: 15 of the 28 words are also named in the prose rows
    above, so deleting a table row left the suite green while the section
    claimed the table was kept complete. A check that cannot see the row it
    describes is decoration.
    """
    start = contract_text.find(TABLE_HEADING)
    if start < 0:
        return frozenset()
    rest = contract_text[start + len(TABLE_HEADING) :]
    end = rest.find("\n## ")
    section = rest if end < 0 else rest[:end]
    return frozenset(m.group(1) for m in TABLE_ROW.finditer(section))


def undocumented(contract_text: str) -> list[str]:
    """Reason words the code can emit that the TABLE does not document.

    Takes the text rather than reading the file, so the controls below can run
    the real checker against a damaged copy.
    """
    return sorted(set(reason_words()) - table_words(contract_text))


def stale_rows(contract_text: str) -> list[str]:
    """Table rows naming a word the code can no longer emit.

    THE OTHER DIRECTION, which the first version of this file did not check at
    all. A table that only ever grows rots exactly as silently as one that is
    short: a reason renamed or removed leaves a row telling the operator to
    expect a refusal they can never receive, and nothing failed.
    """
    return sorted(table_words(contract_text) - set(reason_words()))


def test_the_scanners_could_read_every_site() -> None:
    """FIRST, before any coverage claim: did the instrument manage to read?

    A site the scanner cannot resolve is not a documented reason, and a
    denominator built on a partial read is the defect `refusal_scan.py` was
    written against. This is asserted before the coverage test below so a
    broken scanner reports THAT rather than a clean contract.
    """
    for where, scan in _scans().items():
        assert not scan.unresolved, (
            f"the scanner could not read these sites in {where}, so the "
            f"vocabulary below is not a denominator: {list(scan.unresolved)!r}"
        )


def test_the_denominator_is_not_empty() -> None:
    """A scan that finds nothing would make every coverage check pass.

    This is the control that stops the test above and the test below from
    going green together on a broken extractor: if a rename or a refactor
    makes the scanners find no words at all, that is reported here as a
    measurement failure instead of being read as full coverage.
    """
    words = reason_words()
    assert len(words) >= 25, (
        "expected the refusal vocabulary to be at least 25 words; found "
        f"{len(words)}: {sorted(words)!r}. Either the scanners stopped seeing "
        "their sources or the vocabulary shrank; both need a person."
    )
    for anchor in ("daily_loss", "sl_required", "volume_unusable", "orders_unmeasured"):
        assert anchor in words, (
            f"{anchor!r} is missing from the scan, so at least one source is "
            "no longer being read"
        )


def test_every_refusal_reason_is_documented_in_the_contract() -> None:
    """The gap #220 was filed for, kept closed.

    A reason added to the code without a line in the contract fails HERE, which
    is what turns the table into a live record instead of a snapshot of the day
    it was written.
    """
    text = CONTRACT.read_text(encoding="utf-8")
    missing = undocumented(text)
    assert not missing, (
        f"{len(missing)} refusal reason(s) the desk can emit have no row in "
        f"the {TABLE_HEADING!r} table: {missing!r}. The operator sees these "
        "verbatim, so each needs a row saying what was measured and what they "
        "should do."
    )
    stale = stale_rows(text)
    assert not stale, (
        f"{len(stale)} row(s) name a reason the code can no longer emit: "
        f"{stale!r}. A row for a refusal that cannot happen tells the operator "
        "to expect a reply they will never receive, so a removal has to delete "
        "its row in the same change."
    )


def test_the_coverage_check_can_see_an_undocumented_reason() -> None:
    """POSITIVE CONTROL. A check that has only ever passed proves nothing.

    Runs the real checker against a contract copy with one word removed, and
    requires it to be reported. Without this, a `w not in contract_text` that
    silently always evaluated False would make the test above permanently
    green.
    """
    text = CONTRACT.read_text(encoding="utf-8")
    # DERIVED, NEVER NAMED. An earlier version hard-coded `stops_level` as the
    # victim, which coupled this control to one word: legitimately removing
    # that reason from the code would red the CONTROL rather than the closure,
    # and removing any other word would red nothing here at all.
    victim = sorted(reason_words())[0]
    assert victim in table_words(text), "the table does not document " + victim

    # Remove the ROW, which is what the gate now reads, rather than every
    # mention of the word in the file.
    damaged = re.sub(
        r"^\| `%s:?` \|.*$\n?" % re.escape(victim), "", text, count=1, flags=re.M
    )
    assert table_words(damaged) != table_words(text), "the row was not removed"
    assert victim in undocumented(damaged), (
        "the coverage check did not notice a table row being deleted, so its "
        "clean result on the real contract means nothing"
    )
    # And the undamaged text must still come back clean, so the control is
    # measuring the removal rather than a checker that always finds something.
    assert not undocumented(text)

    # The reverse gate too: a row for a word the code cannot emit is reported.
    invented = text.replace(
        "| `" + victim + "`",
        "| `a_reason_the_code_never_emits` | x | y |\n| `" + victim + "`",
        1,
    )
    assert "a_reason_the_code_never_emits" in stale_rows(invented), (
        "the stale-row check cannot see a row with no code behind it"
    )


def test_prose_refusals_are_pinned_rather_than_ignored() -> None:
    """Not every reply after `refused: ` is a word, and the exceptions are named.

    `desk.py` answers `refused: unresolved send <client_id>`, which is prose
    about one in-flight send. It cannot be a vocabulary entry, so the scanner
    returns it separately. Pinning the exact set is what keeps that from
    becoming a silent ignore list: a new prose refusal fails here.
    """
    prose = tuple(
        sorted(
            p
            for scan in _scans().values()
            for p in scan.forwarded
            if p.startswith("prose: ")
        )
    )
    assert prose == PINNED_PROSE, (
        f"the set of PROSE refusals changed: found {prose!r}, pinned "
        f"{PINNED_PROSE!r}. A new one is either a reason word that should be "
        "named and documented, or prose that belongs in this pin."
    )


#: Module-level `unusable_*` functions in `sizing.py` whose words are
#: DELIBERATELY NOT operator vocabulary, with the reason, so the exclusion is a
#: decision on the record instead of an omission.
#:
#: `unusable_price` (#221) returns `"unreadable"` and `"absent"`. Both are
#: INTERNAL discriminators: `engine.py` consumes them as
#: `unusable_price(value) == "unreadable"` and raises `RuntimeError` with prose
#: ("unreadable tick for EURUSD: bid=nan ..."), so neither word is ever rendered
#: after `refused: `. Documenting them in the contract's refusal table would
#: tell an operator to expect a reply they can never receive.
NOT_OPERATOR_VOCABULARY = frozenset({"unusable_price"})


def _unusable_functions() -> frozenset[str]:
    """Every module-level `unusable_*` function `sizing.py` defines."""
    import ast

    tree = ast.parse((SRC / "sizing.py").read_text(encoding="utf-8"))
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("unusable_")
    )


def test_every_refusal_authority_is_classified() -> None:
    """A NEW authority function must be classified by a person, not guessed.

    `REASON_AUTHORITIES` is a hardcoded tuple, and hardcoding is CORRECT here:
    whether a word reaches the operator is a fact about how `engine.py` renders
    it, which cannot be derived from the function's name. Auto-discovering every
    `unusable_*` function would wrongly demand that `unusable_price`'s internal
    discriminators be documented as refusals.

    But a hardcoded list has the failure mode `scan_decision_reasons` was added
    for: **the population moves and the scanner does not notice.** #221 added a
    third authority beside the two this file was written against, and nothing
    here would have said so.

    So the list is not auto-derived, it is RECONCILED: every `unusable_*`
    function must be either operator vocabulary or explicitly excluded with a
    reason. A fourth one fails here until somebody decides which it is.

    Deliberately tolerant of both trees: this passes whether or not
    `unusable_price` is present, because it asserts that nothing is
    UNCLASSIFIED rather than pinning an exact set, and the set legitimately
    differs across a merge boundary.
    """
    from refusal_scan import REASON_AUTHORITIES

    found = _unusable_functions()
    classified = frozenset(REASON_AUTHORITIES) | NOT_OPERATOR_VOCABULARY
    unclassified = sorted(found - classified)
    assert not unclassified, (
        f"sizing.py defines {unclassified!r}, which is neither in "
        "REASON_AUTHORITIES (its words reach the operator as `refused: "
        "<reason>` and must be in docs/CONTRACT.md) nor in "
        "NOT_OPERATOR_VOCABULARY (its words are internal and must say why). "
        "Decide which, rather than leaving the vocabulary silently short."
    )
    # And the vocabulary authorities must actually EXIST, so a rename cannot
    # quietly empty the list while this test still passes.
    missing = sorted(frozenset(REASON_AUTHORITIES) - found)
    assert not missing, (
        f"REASON_AUTHORITIES names {missing!r}, which sizing.py does not "
        "define; the vocabulary scan is reading nothing for those"
    )
