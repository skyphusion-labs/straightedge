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

import pytest

from refusal_scan import (
    NON_WORD_KINDS,
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

#: Every `refused: ` site that names NO word here, pinned exactly so a NEW one
#: fails and gets looked at by a person. Two kinds, and the pin covers both
#: because the gap that let one through was the kind that was not pinned:
#:
#: * `prose:` a literal with no word shape. `refused: unresolved send <id>`
#:   names one in-flight send and can never be a contract entry.
#: * `interpolated:` an f-string whose head is exactly `refused: `. Either the
#:   word is named elsewhere and another scanner reads it there
#:   (`{decision.reason}`, `{bad}`, `{trip.reason}`, `{reason}`, `{bad_stop}`,
#:   `{exc.reason}`), or the payload is free text nobody defines
#:   (`{result.comment}`, set by `OrderResult.not_sent`). No scanner can tell
#:   those apart by dataflow, so the decision is a person's.
#:
#: MEASURED: before this pin existed, adding a new `f"refused: {x.comment}"`
#: site produced byte-identical scanner output, so the hole sat exactly in the
#: mechanism meant to force that look. A new site of either kind now reds here.
PINNED_NON_WORD_SITES = (
    "interpolated: f'refused: {bad}'",
    "interpolated: f'refused: {bad_stop}'",
    "interpolated: f'refused: {decision.reason}'",
    "interpolated: f'refused: {exc.reason}'",
    "interpolated: f'refused: {reason}'",
    "interpolated: f'refused: {result.comment}'",
    "interpolated: f'refused: {trip.reason}'",
    "prose: refused: unresolved send",
)


def is_non_word_site(site: str) -> bool:
    """THE gate's filter, in ONE place, called by every consumer.

    This existed as the same expression written out at four call sites: the
    pin, the injection test, the survives-the-filter test and the containment
    test. Four correct spellings are still four sources of truth, and the
    review proved what that costs: changing the PIN's line back to a
    hand-copied `("prose: ", "interpolated: ")` left all twenty tests GREEN,
    because the injection test rebuilt an equivalent filter of its own instead
    of calling the pin's. The instrument reading it was not the gate acting on
    it, one level up from where that was first fixed.

    It is invisible on today's tree because no `composed: ` site exists in the
    real sources, so a hand-copied list and the declaration behave identically
    until the day one appears, which is the day it matters.

    A hand-copy HERE is caught by
    `test_each_kind_is_produced_and_survives_the_gate_filter`, which asks
    whether a site of each DECLARED kind survives this predicate.
    """
    return site.startswith(NON_WORD_KINDS)


def non_word_sites(*scans: object) -> tuple[str, ...]:
    """Every non-word site across the given scans, filtered by the predicate."""
    return tuple(
        sorted(
            site
            for scan in scans
            for site in scan.forwarded  # type: ignore[attr-defined]
            if is_non_word_site(site)
        )
    )


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
    #
    # FOUND WITH `TABLE_ROW`, NEVER COMPOSED. The earlier version wrote the row
    # shape out as `"| `" + victim + "`"`, with no optional colon, while the
    # removal half above used the regex that allows one. So a victim that is a
    # PREFIX word, whose row reads `| `word:` |`, matched nothing and this
    # control redded loudly for a reason that had nothing to do with the gate.
    # It passed only because `sorted(reason_words())[0]` happens not to be a
    # prefix word today, which is luck rather than a property.
    #
    # The regex that knows a row may carry a trailing colon is `TABLE_ROW`, in
    # this same file and already used by `table_words`. Adding `:?` to a
    # second hand-written spelling would make two spellings agree by
    # coincidence for the third time in this file; finding the real row means
    # the control never writes the shape down at all.
    row = next(
        (m for m in TABLE_ROW.finditer(text) if m.group(1) == victim), None
    )
    assert row is not None, f"TABLE_ROW does not match the row for {victim!r}"
    invented = (
        text[: row.start()]
        + "| `a_reason_the_code_never_emits` | x | y |\n"
        + text[row.start() :]
    )
    assert "a_reason_the_code_never_emits" in stale_rows(invented), (
        "the stale-row check cannot see a row with no code behind it"
    )


def test_non_word_refusal_sites_are_pinned_rather_than_ignored() -> None:
    """Not every reply after `refused: ` is a word, and every exception is named.

    This is the half that was broken. The first version pinned only `prose: `
    sites, and `scan_refusal_literals` returned early on an f-string head of
    exactly `refused: `, so an INTERPOLATED site was not pinned and was not
    returned at all. Adding a new `f"refused: {x.comment}"` therefore changed
    nothing anywhere, which put the hole inside the one mechanism this module
    exists for: forcing a person to look at a new refusal.

    Both kinds are pinned now. A new site reds here whether its payload is a
    word named elsewhere or free text nobody defines, and the message says
    which decision is owed.
    """
    sites = non_word_sites(*_scans().values())
    # Compared against a SORTED pin rather than the literal order above, so a
    # future editor adding an entry where it reads naturally does not get a
    # spurious failure. `{bad_stop}` sorts before `{bad}` because `_` < `}`,
    # which is exactly the kind of ordering trap that teaches people to stop
    # trusting a pin.
    assert sites == tuple(sorted(PINNED_NON_WORD_SITES)), (
        f"the set of non-word `refused: ` sites changed.\nfound  {sites!r}\n"
        f"pinned {tuple(sorted(PINNED_NON_WORD_SITES))!r}\n"
        "A new site is one of three things, and which one is a person's call: "
        "a reason word that should be named and given a table row; a payload "
        "whose word is named elsewhere and already scanned there; or free text "
        "that can never be a vocabulary entry and belongs in this pin."
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


#: Every spelling of a refusal reply the scanner is claimed to SEE, with the
#: kind it is reported as. Driven as a test per form because the previous two
#: versions of `scan_refusal_literals` each closed one shape and left the next
#: most natural one invisible, while the comment claimed closure.
SEEN_SPELLINGS = (
    ('return f"refused: {x.comment}"', "interpolated: "),
    ('return "refused: " + x.comment', "composed: "),
    ('return "refused:" + x.comment', "composed: "),
    ('return "".join(["refused: ", x.comment])', "composed: "),
    ('return f"{x.p}refused: {x.comment}"', "composed: "),
    ('return "refused: %s" % x.comment', "prose: "),
    ('return "refused: {}".format(x.comment)', "prose: "),
)

#: THE ONE RESIDUAL BLIND SPOT, pinned as a test rather than left in prose.
#: A COMPUTED prefix leaves no literal to find, so no literal scan can see it.
#: Pinned so that closing it later reds this test and forces the docstring and
#: the contract paragraph to be updated with it, instead of the claim quietly
#: becoming true and nobody noticing, or quietly staying false.
UNSEEN_SPELLING = 'return "ref" + "used: " + x.comment'


#: A module with no refusal site in it, so a probe measures the ADDED line and
#: nothing else. Deliberately NOT `desk.py`: reading the real module made each
#: probe a delta against whatever else was in the tree, so injecting one site
#: changed the baseline for every other case and these tests redded on a change
#: they were not about. A gate that reds on an unrelated edit is one the next
#: person disables.
_PROBE_STUB = "def unrelated(x) -> str:\n    return str(x)\n"


def _probe(line: str):
    """`scan_refusal_literals` over a clean stub plus one added refusal site."""
    from refusal_scan import scan_refusal_literals

    before = scan_refusal_literals(_PROBE_STUB)
    after = scan_refusal_literals(
        _PROBE_STUB + "\n\ndef _probe_site(x) -> str:\n    " + line + "\n"
    )
    return (
        sorted(set(after.forwarded) - set(before.forwarded)),
        sorted(set(after.names) - set(before.names)),
    )


@pytest.mark.parametrize("line,kind", SEEN_SPELLINGS, ids=[s[0][:34] for s in SEEN_SPELLINGS])
def test_the_scanner_sees_each_refusal_spelling(line: str, kind: str) -> None:
    """One form per case, so a regression names the spelling it lost.

    The invariant being tested is NOT a list of blessed shapes: it is that a
    refusal reply must contain the literal prefix somewhere in the source, so
    every string constant carrying it is a site whatever assembles the rest.
    These cases are the evidence for that invariant, not its definition.
    """
    sites, names = _probe(line)
    assert sites or names, (
        f"the scanner did not see {line!r} at all, so a refusal written that "
        "way would be invisible to the pin and to the table"
    )
    assert any(s.startswith(kind) for s in sites) or names, (
        f"{line!r} was seen but not as {kind!r}: got {sites!r} / {names!r}"
    )


def test_a_computed_prefix_is_the_known_blind_spot() -> None:
    """PINNED LIMIT, not an oversight.

    A prefix assembled at runtime leaves no literal for an AST scan to find.
    Nobody writes it, and the docstring and `docs/CONTRACT.md` both say so, so
    the claim made there is the true one.

    This test exists so the claim and the code cannot drift apart in either
    direction: if someone closes this hole, this test reds and the two places
    stating the limit have to be updated in the same change.
    """
    sites, names = _probe(UNSEEN_SPELLING)
    assert not sites and not names, (
        "a computed prefix is now visible to the scanner, which is an "
        f"improvement: got {sites!r} / {names!r}. Update the docstring of "
        "scan_refusal_literals and the CONTRACT.md paragraph that both name "
        "this as the residual blind spot, then delete this test."
    )


#: One spelling per kind the scanner can emit, so every declared kind is proved
#: REACHABLE and proved to survive the gate's filter. A kind declared and never
#: produced is a filter entry nobody tests; a kind produced and not filtered is
#: the defect this file already shipped once.
KIND_WITNESSES = {
    "composed: ": 'return "refused: " + x.operator_detail',
    "interpolated: ": 'return f"refused: {x.operator_detail}"',
    "prose: ": 'return "refused: %s" % x.operator_detail',
}


def test_the_gate_covers_every_kind_the_scanner_can_emit() -> None:
    """The claim that would have caught the `composed: ` defect.

    Not "a bucket is empty", which is a weaker and more fragile claim: this
    scanner's `forwarded` legitimately holds eight entries, so empty is not
    available, and a bucket that CAN hold a correctly-recognised input is a
    disposition rather than a residual pile. What holds instead is that the
    gate's filter covers every kind the scanner is able to emit.

    Enforced two ways, because the first alone is not enough. The filter is
    imported from the scanner rather than repeated, so it cannot drift; and
    every declared kind is witnessed below by a spelling that actually produces
    it, so a kind cannot be declared, filtered and dead.
    """
    assert set(KIND_WITNESSES) == set(NON_WORD_KINDS), (
        "the scanner declares kinds this test has no witness for, or vice "
        f"versa: declared {sorted(NON_WORD_KINDS)!r}, witnessed "
        f"{sorted(KIND_WITNESSES)!r}. A kind with no witness is a filter entry "
        "nobody tests."
    )


@pytest.mark.parametrize("kind", sorted(KIND_WITNESSES), ids=lambda k: k.strip(": "))
def test_each_kind_is_produced_and_survives_the_gate_filter(kind: str) -> None:
    """The instrument reads it AND the gate acts on it, which are two claims.

    The `composed: ` defect passed every test in this file while a concatenated
    refusal was surfaced by the scanner and dropped by the gate, because the
    only test that touched it asked whether the SCANNER saw it. This asks the
    second question: does the site survive the filter the pin uses.
    """
    sites, names = _probe(KIND_WITNESSES[kind])
    assert not names, f"{kind!r} witness produced a NAME, so it is the wrong witness: {names!r}"
    assert sites, f"the scanner produced nothing for the {kind!r} witness"
    of_kind = [s for s in sites if s.startswith(kind)]
    assert of_kind, f"expected a {kind!r} site, got {sites!r}"
    survives = [s for s in of_kind if is_non_word_site(s)]
    assert survives == of_kind, (
        f"a {kind!r} site does not survive the gate's filter, so the scanner "
        "would see a new refusal of this shape and the pin would stay green: "
        f"{of_kind!r}"
    )


def test_an_injected_composed_site_would_red_the_pin() -> None:
    """END TO END, against the PIN rather than the scanner.

    The distinction this test exists for: a site being visible to
    `scan_refusal_literals` and a site reaching the pinned comparison are
    different claims, and only the second is the gate. Measured by injection
    into desk.py when this was fixed; asserted here so it stays fixed.
    """
    from refusal_scan import scan_refusal_literals

    injected = (SRC / "desk.py").read_text(encoding="utf-8") + (
        '\n\ndef _injected(x) -> str:\n'
        '    return "refused: " + x.operator_detail\n'
    )
    # CALLS THE PIN'S OWN AGGREGATOR. Rebuilding an equivalent filter here is
    # what made this test look like a check on the gate while being a check on
    # a lookalike, which is the defect the review found.
    sites = non_word_sites(scan_refusal_literals(injected))
    assert sites != tuple(sorted(PINNED_NON_WORD_SITES)), (
        "injecting a concatenated refusal into desk.py leaves the pinned set "
        "unchanged, so the pin would not red and a new free-text refusal is "
        "unforced"
    )
    assert any(s.startswith("composed: ") for s in sites), sites


def test_no_undeclared_kind_escapes_the_scanner() -> None:
    """The third leg, and without it the other two do not close.

    The argument the gate rests on is a containment chain:

      every site the scanner EMITS carries a DECLARED kind   (this test)
      the gate's filter IS the declaration                   (imported, not copied)
      therefore every emitted site reaches the pin

    Leg one is what this test adds. Without it, the first two are satisfied by
    a scanner that emits `"weird: ..."` with a raw literal: the declaration
    would not list it, the filter would therefore not cover it, and the gate
    would be blind again for a new reason while every other test here passed.
    That is the same defect as the `composed: ` one, displaced from the filter
    to the emitter, which is exactly where a fix that only touched the filter
    would leave it.

    Checked over the real modules AND over every witness, since a kind can be
    unreachable in today's source and reachable from a probe.

    THE REACH OF THIS LEG, STATED BECAUSE THE CLAIM AROUND IT WAS STRONGER
    THAN THE INSTRUMENT. It sees an undeclared kind only on a spelling that
    the real sources or one of the witnesses actually reaches. An emitter that
    produced a new kind ONLY for, say, a `.format()` refusal would stay green
    here until `desk.py` contains a `.format` refusal site. That is an
    instrument limit rather than a defect, and it is not closed: closing it
    would need a witness per spelling, which is the enumeration this file
    already decided against.

    So the honest statement of the chain is NOT "the class is impossible". It
    is: for every spelling reached by the real sources or a witness, an
    undeclared kind fails here, a declared kind with no witness fails in
    `test_the_gate_covers_every_kind_the_scanner_can_emit`, and a kind the
    declaration drops fails in
    `test_each_kind_is_produced_and_survives_the_gate_filter`. A file whose
    subject is overclaimed mechanisms does not get to overclaim its own.
    """
    from refusal_scan import scan_refusal_literals

    sources = {
        "desk.py": (SRC / "desk.py").read_text(encoding="utf-8"),
        "engine.py": (SRC / "engine.py").read_text(encoding="utf-8"),
    }
    for kind, line in KIND_WITNESSES.items():
        sources["witness " + kind.strip(": ")] = (
            _PROBE_STUB + "\n\ndef _w(x) -> str:\n    " + line + "\n"
        )

    offenders = {}
    for where, text in sources.items():
        for site in scan_refusal_literals(text).forwarded:
            if not is_non_word_site(site):
                offenders.setdefault(where, []).append(site)

    assert not offenders, (
        "scan_refusal_literals emitted a site whose kind is not in "
        f"NON_WORD_KINDS, so the gate's filter cannot cover it: {offenders!r}. "
        "Either declare the kind (and add a witness) or stop emitting it."
    )
