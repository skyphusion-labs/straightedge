"""Every word `_stop_guard` can refuse with is documented and gated (#228).

#220 closed the `refused: <word>` channel with a scan that has a denominator.
`_stop_guard`'s words are just as operator-visible and were outside every
scanner in `refusal_scan.py`, because they do not appear after `refused: ` and
no `RiskDecision` carries them: the guard returns a bare word, `_modify` hands
it to `OrderResult.invalid_stops`, and the desk renders
`sl failed retcode=<n> <word>`.

THE RECORD WAS ALREADY COMPLETE AND THE GATE WAS MISSING, which is why this
changes no operator-visible behaviour. All three words are documented in the
`/sl` row. What was missing is that nothing READ that documentation, so a
fourth word, or a rename of any of the three, would have reded nothing.

MEASURED WHEN THIS WAS WRITTEN, and it is why the gate is a scan rather than
two assertions: the issue named TWO words, `stop_removal_refused` and
`stop_exceeds_risk`, measured by grepping for those two constants.
`_stop_guard` returns **three**. `spec_not_measured:<fields>` is built by
concatenation rather than named by a constant, so a grep for known names could
not have found it, and an enumeration that starts from the names somebody
already knows is not a denominator. That is the same failure one level up from
the one this issue was filed about.

SCOPE, because a gate that does not say what it cannot see gets read as
covering everything (the ruling on #220's own scope note).

WHAT THE SCAN READS, as a RETURN from `_stop_guard`: a string literal, a module
constant, a literal-prefixed concatenation (`"spec_not_measured:" + ...`), an
f-string opening with a literal, and the branches of `or` and of a conditional
expression.

WHAT IT CANNOT READ, measured by injection rather than reasoned about: a word
returned through a LOCAL variable, a `"".join([...])`, a `str(self._helper())`,
or a concatenation whose left side is not a literal. **Every one of those lands
in `scan.forwarded` instead of being silently dropped**, which is what makes
the gap observable, so `test_the_scanners_could_read_every_site` pins
`forwarded` EMPTY for the guard and pins the factory's forwarded set to exactly
the one site that is meant to forward. A new spelling therefore fails there
rather than passing as covered: the gate cannot read it, and it refuses to
pretend the read happened.

The remaining hole was a result built WITHOUT the factory
(`OrderResult(retcode=..., comment="...")`), which `scan_factory_comments`
cannot see by construction. That is closed structurally rather than excluded:
`engine.py` builds every result through a factory, zero direct constructions,
and `test_every_result_is_built_through_a_factory` keeps it that way.
"""

from __future__ import annotations

import ast
import pathlib
import re

from refusal_scan import ReasonScan, scan_factory_comments, scan_method_reasons

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENGINE = ROOT / "src" / "straightedge" / "engine.py"
CONTRACT = ROOT / "docs" / "CONTRACT.md"

#: The guard whose return values ARE the vocabulary of this channel.
GUARD_CLASS = "Engine"
GUARD_METHODS = ("_stop_guard",)

#: Comments on this channel that are operator PROSE rather than vocabulary,
#: pinned exactly so a NEW one fails here and is looked at by a person instead
#: of joining a list nothing checks.
#:
#: `_modify_pending` refuses a working-order modify with sentences, not words:
#: they describe the ORDERING a working order's price, stop and target must
#: satisfy, which is a relation between three numbers rather than a named
#: condition. A contract cannot enumerate them as vocabulary, and forcing them
#: into a word list would mean inventing three words no code emits.
#:
#: `sl required` is a deliberate exception on the record rather than an
#: oversight: the `refused:` channel documents `sl_required` for the same
#: condition, so the desk has ONE meaning with TWO renderings, one per channel.
#: Normalising that is an operator-visible string change on a documented reply,
#: so it is filed as #233 rather than decided in the change that adds the gate.
PINNED_PROSE = frozenset(
    {
        "sl required",
        "buy needs sl < entry < tp",
        "sell needs tp < entry < sl",
    }
)

#: The exact heading of the section whose table IS the enumeration. A LEVEL-2
#: heading on purpose: #220's section scan takes everything from
#: `### Refusal reasons` to the next `## `, so a `###` table here would be read
#: as part of ITS enumeration and every word below would surface there as a row
#: with no code behind it.
TABLE_HEADING = "## Stop-guard refusals: the modify channel"

#: A table row naming one word: `| `word` |`, or `| `word:` |` when a measured
#: payload follows it at runtime.
TABLE_ROW = re.compile(r"^\| `([a-z][a-z0-9_]*):?` \|", re.M)

#: Methods that forward a guard verdict into `invalid_stops`. Hardcoded, like
#: #220's authority list, because being the carrier is a fact about what the
#: method DOES rather than about its name.
MODIFIERS = ("_modify", "_modify_pending")

#: The verbs that can put one of these words in front of an operator, derived
#: below rather than listed from memory, and pinned so a sixth path fails.
CARRYING_VERBS = frozenset({"sl", "tp", "replace", "be", "trail"})

#: Renders of a result comment that CANNOT carry a guard word, because their
#: methods never reach a modifier. Pinned as the negative half: a derivation
#: that returned every verb would pass the test above and prove nothing.
NON_CARRYING_VERBS = frozenset({"cancel", "close", "closeby"})

RENDER_HEAD = " failed retcode="


def _source() -> str:
    return ENGINE.read_text(encoding="utf-8")


def _scans() -> dict[str, ReasonScan]:
    """Named per question, so a failure says WHICH read came up short.

    The vocabulary and the population are different questions and the gap
    between them is what the prose pin reconciles.
    """
    source = _source()
    return {
        "engine.py (Engine._stop_guard returns)": scan_method_reasons(
            source, class_name=GUARD_CLASS, method_names=GUARD_METHODS
        ),
        "engine.py (OrderResult.invalid_stops comments)": scan_factory_comments(source),
    }


def guard_words() -> frozenset[str]:
    """The vocabulary: every word the guard can return. The denominator."""
    return _scans()["engine.py (Engine._stop_guard returns)"].names


def channel_comments() -> frozenset[str]:
    """The population: every literal comment this channel can carry."""
    return _scans()["engine.py (OrderResult.invalid_stops comments)"].names


def _section_bounds(contract_text: str) -> tuple[int, int]:
    """Byte range of this section, so a WRITER can be scoped like the READER.

    Extracted from `table_words` rather than spelled a second time: a control
    that scopes itself with its own copy of these bounds is the duplicate that
    #220 spent three iterations removing from its own gate.
    """
    start = contract_text.find(TABLE_HEADING)
    if start < 0:
        return (0, 0)
    rest = contract_text[start + len(TABLE_HEADING) :]
    end = rest.find("\n## ")
    stop = len(contract_text) if end < 0 else start + len(TABLE_HEADING) + end
    return (start, stop)


def table_words(contract_text: str) -> frozenset[str]:
    """The words this section's TABLE documents, trailing `:` stripped.

    ANCHORED TO THE SECTION, NOT TO THE FILE. All three words are already named
    in the `/sl` row's prose, so a check asking `word in contract_text` would
    have passed on the tree this test was written against, before the table
    existed at all. That is #220's own measured failure (15 of its 28 words
    were named in prose, so deleting a table row left the suite green), and
    repeating it here would make this gate decoration.
    """
    start = contract_text.find(TABLE_HEADING)
    if start < 0:
        return frozenset()
    rest = contract_text[start + len(TABLE_HEADING) :]
    end = rest.find("\n## ")
    section = rest if end < 0 else rest[:end]
    return frozenset(m.group(1) for m in TABLE_ROW.finditer(section))


def _first_table_row(contract_text: str) -> str:
    """The first table row line of this section, exactly as it is written.

    Used by the control below so an injected row lands inside the section the
    gate reads, and so the injection cannot silently match nothing.
    """
    start = contract_text.find(TABLE_HEADING)
    assert start >= 0, "the section this gate reads is gone"
    rest = contract_text[start + len(TABLE_HEADING) :]
    end = rest.find("\n## ")
    section = rest if end < 0 else rest[:end]
    match = TABLE_ROW.search(section)
    assert match is not None, "the section was found and its table read empty"
    line_start = section.rfind("\n", 0, match.start()) + 1
    line_end = section.find("\n", match.start())
    return section[line_start:] if line_end < 0 else section[line_start:line_end]


def undocumented(contract_text: str) -> list[str]:
    """Words the guard can return that the table does not document.

    Takes the text rather than the path, so the controls can run the real
    checker against a damaged copy.
    """
    return sorted(guard_words() - table_words(contract_text))


def stale_rows(contract_text: str) -> list[str]:
    """Rows naming a word the guard can no longer return.

    The other direction. A table that only grows rots as silently as one that
    is short: a renamed word leaves a row telling the operator to expect a
    refusal they can never receive, and nothing fails.
    """
    return sorted(table_words(contract_text) - guard_words())


def carrying_verbs() -> frozenset[str]:
    """The verbs whose render can carry a guard word, DERIVED from the source.

    A method qualifies when it does both things: it calls `_modify` or
    `_modify_pending`, and it renders `<verb> failed retcode=`. Derived rather
    than listed, because the issue and the first draft of this file both said
    "the `sl failed` channel" and the measurement says five verbs. `_act` and
    `_manage_open` call a modifier and render nothing, which is correct: on the
    auto path the refusal is journaled as `modify_refused` and no operator is
    waiting on a reply.
    """
    tree = ast.parse(_source())
    verbs: set[str] = set()
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef) or cls.name != GUARD_CLASS:
            continue
        for node in ast.walk(cls):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            heads: list[str] = []
            reaches = False
            for sub in ast.walk(node):
                if isinstance(sub, ast.JoinedStr) and sub.values:
                    head = sub.values[0]
                    if (
                        isinstance(head, ast.Constant)
                        and isinstance(head.value, str)
                        and RENDER_HEAD in head.value
                    ):
                        heads.append(head.value)
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in MODIFIERS
                ):
                    reaches = True
            if reaches:
                verbs.update(h.split(RENDER_HEAD)[0] for h in heads)
    return frozenset(verbs)


def test_the_scanners_could_read_every_site() -> None:
    """FIRST, before any coverage claim: did the instrument manage to read?

    A return or a comment the scanner could not resolve is not a documented
    word, and a denominator built on a partial read is what `refusal_scan.py`
    exists to prevent. A renamed `_stop_guard` reports here, as a measurement
    failure, rather than silently emptying the vocabulary.
    """
    for where, scan in _scans().items():
        assert not scan.unresolved, (
            f"the scanner could not read these sites in {where}, so the "
            f"vocabulary below is not a denominator: {list(scan.unresolved)!r}"
        )

    # AND NOTHING MAY BE FORWARDED OUT OF THE GUARD. A word returned through a
    # local variable, a `"".join([...])`, a `str(...)` call or a concatenation
    # with a non-literal left side cannot be read as a word; each lands here
    # rather than being dropped, so pinning this EMPTY turns every one of those
    # spellings into a failure instead of a silent gap. Measured by injection:
    # all four leave the rest of this file green and show up only here.
    guard = _scans()["engine.py (Engine._stop_guard returns)"]
    assert guard.forwarded == (), (
        "the guard now returns a word the scan cannot read as a word: "
        f"{guard.forwarded!r}. Either return a literal or a module constant, "
        "or teach `refusal_scan` that spelling; it cannot be documented as "
        "vocabulary while it is unreadable."
    )

    # The factory forwards from EXACTLY one site, the `_modify` hand-off. A
    # second forwarded name means a new comment is being passed in from
    # somewhere the scan cannot name, which is the same gap one call over.
    factory = _scans()["engine.py (OrderResult.invalid_stops comments)"]
    assert factory.forwarded == ("reason",), (
        "the set of sites passing a non-literal comment to invalid_stops "
        f"changed: {factory.forwarded!r}. Exactly one is expected, the guard "
        "verdict `_modify` forwards; anything else needs reading."
    )


def test_every_result_is_built_through_a_factory() -> None:
    """The one hole the comment scan cannot see, closed by structure.

    `scan_factory_comments` reads `OrderResult.invalid_stops(...)`. A result
    built with the constructor directly, `OrderResult(retcode=10016,
    comment="zz")`, would carry a word to the operator through the same render
    and be invisible to it. Measured on the tree this was written against:
    `engine.py` contains ZERO direct constructions and reaches every result
    through `measured`, `unchanged`, `not_sent` or `invalid_stops`, so the hole
    is closed by keeping that true rather than by writing an exclusion that
    nothing enforces.
    """
    tree = ast.parse(_source())
    direct = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "OrderResult"
    ]
    assert not direct, (
        f"engine.py builds an OrderResult directly at line(s) {direct!r}. Its "
        "`comment` reaches the operator through the same `<verb> failed "
        "retcode=` render as a guard word and is invisible to the comment "
        "scan, so use a factory or extend the scan."
    )


def test_the_denominator_is_not_empty_and_the_channel_is_still_wired() -> None:
    """The control that stops this file from going green on a broken read.

    A scan finding nothing would make every coverage check below pass. Two
    things are asserted: the words are there, and the WIRING that puts them in
    front of an operator is there. The second matters because if `_modify`
    stopped forwarding the guard's verdict into `invalid_stops`, the words would
    still be returned, still be documented, and never reach anybody, and a gate
    over a channel nobody reads is worse than no gate at all.
    """
    words = guard_words()
    assert len(words) >= 3, (
        f"expected at least 3 stop-guard words; found {len(words)}: "
        f"{sorted(words)!r}. Either the scanner stopped reading the guard or "
        "the vocabulary shrank, and both need a person."
    )
    for anchor in ("stop_removal_refused", "stop_exceeds_risk", "spec_not_measured"):
        assert anchor in words, (
            f"{anchor!r} is missing from the scan, so the guard is no longer "
            "being read as this test assumes"
        )
    forwarded = _scans()["engine.py (OrderResult.invalid_stops comments)"].forwarded
    assert "reason" in forwarded, (
        "no `invalid_stops(reason)` site was found, so the guard's words may "
        f"no longer reach the operator at all; forwarded: {forwarded!r}"
    )


def test_every_stop_guard_word_is_documented_in_the_contract() -> None:
    """The gate #228 was filed for, in both directions.

    A fourth word added to the guard without a row fails here, and so does a
    row naming a word the guard can no longer return.
    """
    text = CONTRACT.read_text(encoding="utf-8")
    missing = undocumented(text)
    assert not missing, (
        f"{len(missing)} stop-guard refusal word(s) have no row in the "
        f"{TABLE_HEADING!r} table: {missing!r}. The operator sees these "
        "verbatim after `retcode=`, so each needs a row saying what was "
        "measured and what they should do."
    )
    stale = stale_rows(text)
    assert not stale, (
        f"{len(stale)} row(s) name a word the guard can no longer return: "
        f"{stale!r}. A row for a refusal that cannot happen tells the operator "
        "to expect a reply they will never receive."
    )


def test_the_coverage_check_can_see_an_undocumented_word() -> None:
    """POSITIVE CONTROL. A check that has only ever passed proves nothing.

    Runs the real checker against a damaged copy of the contract and requires
    the damage to be reported, then requires the undamaged copy to come back
    clean, so the control is measuring the removal rather than a checker that
    always finds something.
    """
    text = CONTRACT.read_text(encoding="utf-8")
    # DERIVED, NEVER NAMED, so legitimately retiring one word reds the closure
    # rather than this control.
    victim = sorted(guard_words())[0]
    assert victim in table_words(text), "the table does not document " + victim

    # REMOVED FROM THIS SECTION, not from the file. The reader above is
    # section-scoped and this removal was not, which is the same whole-file
    # versus section mismatch #220 found in its own gate, mirrored: there the
    # READER was unscoped, here the WRITER was.
    #
    # Latent on the tree this was written against, because `spec_not_measured`
    # had exactly one row in the file. #220 adds a `### Refusal reasons` table
    # that documents the same word EARLIER in the document, so a whole-file
    # `count=1` deletes THAT row, this section keeps its own, and the control
    # reds with "the row was not removed" while nothing is wrong with either
    # table. Measured on the merge: rows at lines 156 and 259, this section
    # spanning 219 to 276, so the deleted one sat outside it.
    sec_start, sec_end = _section_bounds(text)
    row = next(
        (
            m
            for m in TABLE_ROW.finditer(text)
            if m.group(1) == victim and sec_start <= m.start() <= sec_end
        ),
        None,
    )
    assert row is not None, f"no row for {victim!r} inside {TABLE_HEADING!r}"
    line_end = text.find("\n", row.start())
    damaged = text[: row.start()] + text[line_end + 1 :]
    assert table_words(damaged) != table_words(text), "the row was not removed"
    assert victim in undocumented(damaged), (
        "the coverage check did not notice a table row being deleted, so its "
        "clean result on the real contract means nothing"
    )
    assert not undocumented(text)

    # INSERTED AT THE REAL FIRST ROW, found by the same regex the gate reads
    # with, rather than by rebuilding the victim's row text. A row is `|
    # `word` |` for a plain word and `| `word:` |` for a prefix, so composing
    # "| `" + victim + "`" misses every prefix word: on this channel the
    # alphabetically first word IS `spec_not_measured:`, so the injection
    # matched nothing, `stale_rows` returned an empty list, and this control
    # passed over a check that had done nothing. It failed loudly only because
    # the assertion looks for the invented word rather than for a non-empty
    # result.
    first_row = _first_table_row(text)
    invented = text.replace(
        first_row,
        "| `a_word_the_guard_never_returns` | x | y |\n" + first_row,
        1,
    )
    assert "a_word_the_guard_never_returns" in stale_rows(invented), (
        "the stale-row check cannot see a row with no code behind it"
    )


def test_the_table_is_anchored_to_its_own_section() -> None:
    """The anchor, not the file, and the section cannot be the whole document.

    Two failure modes, both measured on #220 rather than imagined. If the
    heading is renamed, `table_words` reads nothing and the test above fails
    LOUD instead of passing on an empty set. And if the section scan ran to the
    end of the file it would pick up the `refused:` table's rows, every one of
    which the guard cannot return, so the stale-row direction would red on a
    correct contract.
    """
    text = CONTRACT.read_text(encoding="utf-8")
    assert TABLE_HEADING in text, "the section this gate reads is gone"
    assert table_words(text), "the section was found and its table read empty"
    assert "daily_loss" not in table_words(text), (
        "the section scan reached beyond its own `##` block and picked up "
        "another channel's table, so the stale-row check would fail on a "
        "correct contract"
    )


def test_prose_comments_are_pinned_rather_than_ignored() -> None:
    """Not every comment on this channel is a word, and the exceptions are named.

    RECONCILIATION, not pattern matching. Nothing here asks whether the text
    LOOKS like a word; the population minus the vocabulary must be exactly the
    pinned set. A new comment is therefore either a guard word that needs a row
    or prose that needs a decision in this pin, and it cannot be neither.
    """
    extra = channel_comments() - guard_words()
    assert extra == PINNED_PROSE, (
        f"the set of non-vocabulary comments on this channel changed: found "
        f"{sorted(extra)!r}, pinned {sorted(PINNED_PROSE)!r}. A new one is "
        "either a refusal word that belongs in the table or prose that belongs "
        "in this pin, and somebody has to say which."
    )


def test_the_verbs_that_can_carry_a_word_are_derived_and_pinned() -> None:
    """Which replies can show one of these words, measured from the source.

    The issue called this "the `sl failed` channel" and so did the first draft
    of the contract section. It is five verbs: `_modify` is also reached by
    `/tp`, `/replace`, `/be` and `/trail`, so the same word can arrive behind
    four other labels. A sixth path fails here.

    The negative half is the half that makes it evidence: `cancel`, `close` and
    `closeby` render a result comment the same way and can never carry a guard
    word, because their methods do not reach a modifier. A derivation that
    returned every render would satisfy the first assertion and prove nothing.
    """
    verbs = carrying_verbs()
    assert verbs == CARRYING_VERBS, (
        f"the set of replies that can carry a stop-guard word changed: found "
        f"{sorted(verbs)!r}, pinned {sorted(CARRYING_VERBS)!r}. Either a new "
        "command reaches a modifier, or one stopped, and the contract section "
        "names this set."
    )
    overlap = verbs & NON_CARRYING_VERBS
    assert not overlap, (
        f"{sorted(overlap)!r} cannot carry a guard word but the derivation "
        "returned it, so the derivation is not discriminating"
    )
