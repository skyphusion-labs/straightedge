"""Issue #263: the censuses detect drift, and nothing ran any of them.

THREE censuses now, not the two #263 found: `encoding_census` joined them
in straightedge#307 and is invoked here for the reasons below, which were
written for the first two and needed no adjustment. The title of this file
is the contract: a census in this repo has an invoker or it is a dead
letter.

`tests/double_census.py` (#244) and `tests/timing_census.py` (#262) are good
instruments and were dead letters: neither is a CI step, and neither is
collected by pytest, because neither is named `test_*`. So a new unmarked
elapsed-time test, or a new seam whose only doubles cannot fail, landed green
and joined the population silently -- which is the exact condition both
censuses were built because the HAND-KEPT version of their list had. The
derived list cannot be left to a person either.

WHY COLLECTED AS TESTS rather than a CI step, which is #263's option 3.

Riding the existing suite adds no required status check, and this repo has
`strict_required_status_checks_policy: true`, so every new required context is
paid again on every current of every other branch in the queue. The suite is
already a required context, so the censuses become gates here for free.

And it converts a report into an assertion. A census that prints a finding and
exits non-zero needs a reader; an assertion needs nobody. That is the same
print-versus-assert distinction as asserting on a journal record rather than
on the chat text.

I originally did NOT name them `test_*` on purpose, and the reason was worth
less than it looked: a census reads the suite, so collecting it inside the
suite felt self-referential. It is not a problem in practice -- these read
source text with `ast`, they do not import or execute the tests they describe,
so there is no ordering or state coupling to get wrong.

AND ONE CORRECTION TO MY OWN RECOMMENDATION, recorded because it was wrong in
a way worth keeping. I argued that `double_census` was NOT gateable because it
exits 1 today on three blind seams, and that gating it would red `main` on a
pre-existing finding. That was right about the exit code and wrong about the
only available shape: an EQUALITY pin on the known set is not an allowlist.
A silent ignore list is the #61 defect, where a scanner that skips what it
cannot resolve stops being a denominator. An asserted set is the opposite --
it fails in BOTH directions, so it cannot rot quietly, and it is the pattern
`tests/test_refusal_reasons.py` already uses with `UNREACHABLE` for the same
reason. The precedent was in the repo and I had not weighed it.
"""

from __future__ import annotations

import pathlib

import double_census
import encoding_census
import timing_census

#: Seams that have doubles but no double that can FAIL, as measured today.
#:
#: PINNED, not allowlisted. Asserted for EQUALITY below, so this fails when a
#: new seam goes blind AND when one of these gains a failing double. Either way
#: a person edits this line deliberately and says why in the commit, which is
#: the whole difference from a list the tool skips quietly.
#:
#: EMPTY as of straightedge#264, which answered the three that were here
#: (`cancel`, `check_working`, `working`). All three took answer 1 from
#: `docs/TESTING.md`, make the double enter the state, and the reason they
#: share one answer is that they share one MECHANISM: all three are
#: `self._result(self._call(op, ...))` in `Mt4Broker`, and `_call` is the
#: injected transport whose mailbox bridge raises `BridgeTimeout`. There was
#: never a case for disclaiming a path that one real exception reaches through
#: all three.
#:
#: What differs, and why it is three tests rather than one parametrised over a
#: list of method names: the CONSEQUENCE. Measured per seam before choosing.
#:
#:   working        a SEND. `inflight.begin()` has already run, so the entry
#:                  must stay OPEN and the next confirm of the same staged
#:                  order must be refused as unresolved.
#:                  tests/test_send_idempotency.py
#:   check_working  a READ, one call EARLIER. `begin()` has NOT run, so the
#:                  ledger must stay EMPTY and nothing may be sent. The
#:                  opposite correct answer to `working`, which is what makes
#:                  the pair worth having.
#:                  tests/test_send_idempotency.py
#:   cancel         a sweep step on the SAFETY path. The order is a survivor,
#:                  the report is incomplete, and the record must say COULD
#:                  NOT MEASURE rather than carry a venue retcode.
#:                  tests/test_flatten.py
#:
#: An empty set is not a finished job: it means no seam a double implements is
#: blind. A seam no double implements at all is invisible to this census, which
#: is a different question and not one this pin answers.
KNOWN_BLIND_SEAMS: frozenset[str] = frozenset()


def test_every_elapsed_time_assertion_is_marked_timing() -> None:
    """A new test that asserts on a wall-clock budget must say so.

    straightedge#258: the suite could not distinguish a test whose correctness
    requires a budget to be MET from one whose correctness is independent of
    timing, and ~15% of `windows-latest` runs went red on `main` with nothing
    wrong. The marks are durable because they live in the test files; this is
    what notices a new member.
    """
    rows = timing_census.collect()
    assert rows, (
        "the timing census found NO elapsed-time assertions anywhere, which "
        "means the instrument stopped working rather than the population "
        "being empty: there were 14 when this was written"
    )
    unmarked = sorted((f, fn) for f, fn, _, marked in rows if not marked)
    assert not unmarked, (
        "these tests assert on a wall-clock budget without saying so:\n  "
        + "\n  ".join(f"{f}::{fn}" for f, fn in unmarked)
        + "\n\nAdd @pytest.mark.timing, or make the assertion independent of "
        "timing the way #262 did for the ttl case. Run "
        "`python3 tests/timing_census.py` for the directions."
    )


def _new_blind_seam_message(new: list[str]) -> str:
    """The text a reader is shown when a new blind seam lands.

    A FUNCTION rather than an inline f-string so the property "this message
    carries the narrowed claim" can be asserted by CALLING it. The gate it
    belongs to only reds when a new blind seam appears, and the property has
    to hold on every run, so it cannot be verified by waiting for the red.

    THE CLAIM COMES FROM THE CENSUS, never a second copy here: this message
    was the last place still printing the retracted stronger wording, and it
    is the only place a tripped reader ever looks.
    """
    return (
        "these seams have doubles and NO double that can fail:\n  "
        + "\n  ".join(new)
        + "\n\n"
        + double_census.BLIND_SEAM_CLAIM
        + " Make a double enter the state, disclaim the path in the test, or "
        "cite a live run. See docs/TESTING.md."
    )


def test_the_blind_seams_are_exactly_the_known_set() -> None:
    """A new seam whose doubles cannot fail must not land silently.

    straightedge#244: a double that only ever RETURNS cannot enter a failure
    state, so no DOUBLE in this suite covers a failure path through that seam.
    Narrowed in #275 and again here: "no double" is not "nothing", because a
    test that makes the REAL implementation fail covers the path and the census
    cannot see it.
    """
    implemented = double_census.collect()
    assert implemented, (
        "the double census found NO seam implemented by any test double, "
        "which means the instrument stopped working rather than the suite "
        "having no doubles: there were 14 seams when this was written"
    )
    blind = double_census.blind_seams(implemented)

    new = sorted(blind - KNOWN_BLIND_SEAMS)
    assert not new, _new_blind_seam_message(new)

    fixed = sorted(KNOWN_BLIND_SEAMS - blind)
    assert not fixed, (
        "these seams are no longer blind, which is good news this pin has to "
        "be told about:\n  "
        + "\n  ".join(fixed)
        + "\n\nRemove them from KNOWN_BLIND_SEAMS. The pin is asserted for "
        "equality on purpose, so progress updates it rather than hiding in it."
    )


def test_the_blind_seam_detector_can_tell_the_two_cases_apart() -> None:
    """Prove the detector discriminates, rather than trusting that it does.

    Both assertions above are only worth their green if `blind_seams` can
    actually distinguish a double that can raise from one that cannot. Fed
    both shapes directly, with no file reading involved.
    """
    cannot = {"seam": [("test_x.py", "Dummy", False)]}
    can = {"seam": [("test_x.py", "Dummy", True)]}
    mixed = {"seam": [("test_x.py", "A", False), ("test_x.py", "B", True)]}

    assert double_census.blind_seams(cannot) == {"seam"}
    assert double_census.blind_seams(can) == set()
    assert double_census.blind_seams(mixed) == set(), (
        "one double that can fail is enough to make the seam not blind"
    )


#: The wording #275 retracted. It claimed a blind seam means NOTHING covers the
#: failure path, when it means no DOUBLE here does; a test that makes the REAL
#: implementation fail covers it and the census cannot see that.
#:
#: Pinned as a string to be absent rather than as a list of files to check,
#: because the defect was one claim with FIVE spellings and a file list would
#: have to be kept in step with wherever the sixth one lands.
#: ASSEMBLED FROM FRAGMENTS, NOT WRITTEN OUT, and that is not obfuscation.
#: The first version of this pin spelled the needles literally and the scan
#: reded on THIS FILE, because a scanner that looks for a string cannot hold
#: that string in the source it scans. Same self-inclusion as the contract
#: table that counted its own rows: the instrument changed the population it
#: was measuring.
#:
#: Excluding this file by name was the other option and it is worse: the file
#: carrying the pin is exactly where a sixth spelling would be most likely to
#: be pasted, and the control below asserts this file IS scanned.
RETRACTED_CLAIM_WORDINGS = (
    "covers no" + " failure path",
    "cannot cover a" + " failure path",
)

#: `CHANGELOG.md` is append-only record and legitimately quotes the retracted
#: wording while describing the retraction, so it is excluded BY NAME with that
#: reason rather than by a pattern that would also excuse a live file.
CLAIM_SCAN_EXCLUDES = frozenset({"CHANGELOG.md"})


def _scanned_files() -> list[pathlib.Path]:
    root = pathlib.Path(__file__).resolve().parents[1]
    out: list[pathlib.Path] = []
    for pattern in ("*.md", "docs/*.md", "tests/*.py", "src/straightedge/*.py",
                    "src/straightedge/broker/*.py"):
        out.extend(root.glob(pattern))
    return [f for f in sorted(set(out)) if f.name not in CLAIM_SCAN_EXCLUDES]


def test_the_retracted_claim_wording_appears_nowhere_live() -> None:
    """One claim, one wording, enforced repo-wide rather than per file.

    #275 narrowed this claim in ONE of five places. The four it missed were
    this module's docstring, the census module's docstring short-form, the
    document's CLAIM line, and **this file's assertion message, which is the
    one a reader actually sees when the gate reds** -- so the retracted wording
    was the only version a tripped reader was shown.

    Scanning for the retracted STRING rather than checking a list of known
    sites is deliberate: the defect was a claim with five spellings, and a file
    list would itself need keeping in step with wherever the sixth appears.
    """
    offenders: dict[str, list[str]] = {}
    for f in _scanned_files():
        text = f.read_text(encoding="utf-8")
        for bad in RETRACTED_CLAIM_WORDINGS:
            if bad in text:
                offenders.setdefault(str(f.name), []).append(bad)
    assert not offenders, (
        "the retracted claim wording is live again: "
        + repr(offenders)
        + ". A blind seam means no DOUBLE here covers the failure path, never "
        "that nothing does, because a test can make the REAL implementation "
        "fail and this census cannot see it. Use "
        "double_census.BLIND_SEAM_CLAIM for runtime wording."
    )


def test_the_scan_can_find_the_retracted_wording() -> None:
    """CONTROL. The scan above has only ever passed, which proves nothing.

    Without this, `bad in text` could be checking an empty file list and the
    test would be permanently green: the file-collection half is exactly where
    a scan like this goes blind, and the entry in `docs/TESTING.md` about
    printing the denominator is about this shape.
    """
    files = _scanned_files()
    # The scan must be able to FIND the needle, shown on a synthetic string
    # built here rather than on a file, so the control cannot be satisfied by
    # the needle being unfindable.
    sentinel = RETRACTED_CLAIM_WORDINGS[0]
    haystack = "a suite that " + sentinel + " through that seam"
    assert sentinel in haystack, "the needle cannot be found even when present"
    assert all(w not in pathlib.Path(__file__).resolve().read_text(encoding="utf-8")
               for w in RETRACTED_CLAIM_WORDINGS), (
        "a needle is spelled literally in this file, so the scan will red on "
        "itself; assemble it from fragments"
    )
    # And the scanned set must include the files that carried the defect, so a
    # glob change that stops reaching them cannot pass this silently.
    #
    # BY NAME, AND NO COUNT COMPARED TO A CONSTANT. An earlier version of this
    # control asserted `len(files) >= 20`, which is the same error as gating a
    # branch on a check-run ROW COUNT: the number of files under these globs is
    # a property of how many docs and modules the repo happens to have, so it
    # drifts for reasons that have nothing to do with whether the scan reaches
    # the files that carried the defect. Naming them answers that directly, and
    # it already proves the denominator is not empty. The count stays as context
    # in the failure text, never as the criterion.
    names = {f.name for f in files}
    missing = [r for r in ("double_census.py",
                           "test_the_censuses_have_an_invoker.py",
                           "TESTING.md") if r not in names]
    assert not missing, (
        f"the scan no longer reaches {missing}, so a retracted wording could "
        f"return to the files that carried it and stay green "
        f"({len(files)} files scanned)"
    )


def test_the_reader_facing_message_carries_the_narrowed_claim() -> None:
    """INVOKED, not grepped, and the first version of this test was decorative.

    It read this module's own source for the string
    `double_census` + `.BLIND_SEAM_CLAIM` and asserted it was present. That
    assertion cannot go red: the needle is spelled in the very file being
    read, so it always finds ITSELF. A mutation sweep leg that replaced the
    shared reference with a hand-written second copy passed it green, which is
    how the hole was found rather than reasoned about.

    The fix is to stop scanning text and build the message instead. This also
    asserts the RIGHT property: what matters is that the sentence a reader is
    shown is the narrowed one, not which Python expression produced it.
    """
    msg = _new_blind_seam_message(["broker.send"])

    assert double_census.BLIND_SEAM_CLAIM in msg, (
        "the message a tripped reader sees no longer carries the shared "
        "claim, so it is a second spelling of it again:\n" + msg
    )
    assert "broker.send" in msg, (
        "the message does not name the offending seam, so a reader cannot act "
        "on it"
    )
    assert "no DOUBLE here covers" in double_census.BLIND_SEAM_CLAIM, (
        "the shared claim lost its narrowing, which is the whole point of it"
    )
    for bad in RETRACTED_CLAIM_WORDINGS:
        assert bad not in msg, f"the reader-facing message overclaims: {bad}"


# --- straightedge#307: the encoding census ---------------------------------
#
# WHY THESE LIVE HERE and not in a file of their own: this file already IS the
# answer to "a census with no invoker is a dead letter", and `encoding_census`
# is a census. Splitting it out would reproduce the #263 defect one file over.


def test_every_text_mode_file_operation_names_an_encoding() -> None:
    """THE GATE for straightedge#307.

    `Path.read_text()` with no `encoding=` decodes through the platform
    locale, which is cp1252 on the `windows-latest` leg. There were 40 such
    sites in `tests/` when this landed and the suite was green, because
    nothing those sites read happened to carry a byte cp1252 rejects. What
    kept `CHANGELOG.md` ASCII-clean was the repo's no-em-dash STYLE rule,
    doing encoding-safety work nobody assigned it. A style rule is not a
    guard, which is the whole reason this assertion exists.
    """
    c = encoding_census.collect()

    assert c.files_scanned > 0, (
        "the encoding census scanned NO files, so a clean result means the "
        "instrument stopped working rather than the population being empty"
    )
    assert c.total > 0, (
        f"the encoding census found NO text-IO call sites at all across "
        f"{c.files_scanned} files, which is not credible: there were 280 when "
        f"this was written. The scanner, not the repo, is what changed."
    )
    assert not c.unresolved, (
        "the encoding census could not CLASSIFY these call sites, which is a "
        "finding about the scanner and not a clean result:\n  "
        + "\n  ".join(c.unresolved)
    )
    assert not c.findings, encoding_census.findings_message(c.findings)


def test_the_encoding_census_can_see_an_offender(tmp_path: pathlib.Path) -> None:
    """CONTROL. The gate above has only ever passed, which proves nothing.

    Every shape here is one the scanner must get right, and three of them are
    here because the FIRST version of this census got them wrong and this
    control is what caught it. That version looked for any string argument
    whose characters were all in `rwxabt+U` and called it the mode, so
    `open("x", "rb")` read its FILENAME as the mode and `write_text("ab")`
    was dismissed as binary -- a false negative in the only direction that
    matters. Classification is by signature POSITION now.

    Synthesized in `tmp_path` rather than by editing a file under `src/`,
    because a control that requires a person to break the repo first is a
    control that does not get run.
    """
    cases = [
        ("findings", "a plain read_text",
         "import pathlib\npathlib.Path('x').read_text()\n"),
        ("findings", "a plain write_text",
         "import pathlib\npathlib.Path('x').write_text('d')\n"),
        ("findings", "the builtin open in text mode", "open('x')\n"),
        ("findings", "read_text on a fixture expression",
         "import pathlib\nd = pathlib.Path('.')\n(d / 'j.jsonl').read_text()\n"),
        ("findings", "a FILENAME that looks like a mode string", "open('rb')\n"),
        ("findings", "write_text of a mode-shaped string",
         "import pathlib\npathlib.Path('f').write_text('ab')\n"),
        ("named", "an encoding named by keyword",
         "import pathlib\npathlib.Path('x').read_text(encoding='utf-8')\n"),
        ("named", "an encoding named POSITIONALLY",
         "import pathlib\npathlib.Path('x').read_text('utf-8')\n"),
        ("named", "encoding='locale', deliberate and explicit",
         "import pathlib\npathlib.Path('x').read_text(encoding='locale')\n"),
        ("binary", "binary mode positionally", "open('x', 'rb')\n"),
        ("binary", "binary mode by keyword", "open('x', mode='wb')\n"),
        ("exempt_api", "os.open, which has no encoding parameter",
         "import os\nos.open('x', os.O_RDONLY)\n"),
        ("unresolved", "a mode this census cannot read", "m = 'r'\nopen('x', m)\n"),
        ("unresolved", "a shadowed open", "from gzip import open\nopen('x')\n"),
        ("unresolved", "another module's open", "import gzip\ngzip.open('x')\n"),
    ]

    for i, (bucket, label, source) in enumerate(cases):
        d = tmp_path / f"case{i}"
        d.mkdir()
        (d / "synthetic.py").write_text(source, encoding="utf-8")
        c = encoding_census.scan(roots=[d])
        landed = sorted(
            k for k in ("findings", "named", "binary", "exempt_api", "unresolved")
            if getattr(c, k)
        )
        assert landed == [bucket], (
            f"{label}: the census put it in {landed or ['no bucket']} rather "
            f"than {bucket!r}. Source:\n{source}"
        )

    # And `read_bytes` must not be a candidate at all: it never decodes, so
    # demanding an encoding of it would be a false positive the fixer cannot
    # satisfy.
    d = tmp_path / "bytes"
    d.mkdir()
    (d / "synthetic.py").write_text(
        "import pathlib\npathlib.Path('x').read_bytes()\n", encoding="utf-8"
    )
    c = encoding_census.scan(roots=[d])
    assert c.total == 0, f"read_bytes was treated as a text-IO site: {c}"


def test_the_encoding_census_refuses_an_empty_denominator(
    tmp_path: pathlib.Path,
) -> None:
    """A census that measured nothing must never read as "nothing is wrong".

    Same property `double_census.collect` raises `RuntimeError` for. Without
    it, a glob that stops matching turns this gate permanently green, which is
    the failure mode every census in this repo was written against.
    """
    import pytest

    with pytest.raises(RuntimeError, match="NOTHING CHECKED"):
        encoding_census.scan(roots=[tmp_path])


def test_an_encoding_less_read_of_utf8_really_does_misread_under_cp1252(
    tmp_path: pathlib.Path,
) -> None:
    """THE HAZARD ITSELF, asserted rather than cited.

    The census above is only worth having if the defect it hunts is real. This
    proves it on synthesized bytes, and it pins BOTH failure modes because
    they need different reactions from a reader:

      RAISE   `UnicodeDecodeError` naming a byte offset. Loud, and the easier
              one, because something tells you.
      MISREAD no exception at all, wrong text. This is the one that matters:
              it cannot be found by looking at whether CI went red.

    Platform-independent on purpose. It passes `encoding="cp1252"` explicitly
    instead of relying on the runner's locale, so the POSIX legs assert the
    same property the Windows leg would meet, and the test cannot quietly
    become a no-op on the platform where it is cheap to run.
    """
    # --- the RAISE half ----------------------------------------------------
    # U+FB01 LATIN SMALL LIGATURE FI is ef ac 81 in utf-8. 0x81 is UNDEFINED
    # in cp1252. This is not hypothetical: `#197`'s case-mapping corpus put
    # this exact character into a file in `tests/`.
    ligature = tmp_path / "ligature.txt"
    ligature.write_text("ligature: \ufb01\n", encoding="utf-8")
    raw = ligature.read_bytes()
    assert b"\x81" in raw, (
        "this control depends on an undefined cp1252 byte being present and "
        f"it is not: {raw.hex()}"
    )
    assert ligature.read_text(encoding="utf-8") == "ligature: \ufb01\n", (
        "the utf-8 read did not round-trip, so the control is measuring "
        "something other than the decoder"
    )
    try:
        ligature.read_text(encoding="cp1252")
    except UnicodeDecodeError as exc:
        assert "undefined" in exc.reason, exc.reason
    else:
        raise AssertionError(
            "cp1252 decoded a byte that is undefined in cp1252, so this "
            "control can no longer produce its positive and the census above "
            "is guarding nothing"
        )

    # --- the SILENT MISREAD half, the worse one ----------------------------
    # Every byte of a utf-8-encoded Latin-1 character IS defined in cp1252, so
    # there is no exception to notice: the text simply comes back wrong.
    accented = tmp_path / "accented.txt"
    truth = "caf\u00e9 na\u00efve r\u00e9sum\u00e9\n"
    accented.write_text(truth, encoding="utf-8")
    assert b"\x81" not in accented.read_bytes(), (
        "this half must NOT rely on an undefined byte, or it is just the "
        "raise case again"
    )
    misread = accented.read_text(encoding="cp1252")
    assert misread != truth, (
        "cp1252 returned the correct text, so there is no silent misread to "
        "guard against"
    )
    assert len(misread) > len(truth), (
        f"expected mojibake longer than the truth, got {misread!r}"
    )
    assert accented.read_text(encoding="utf-8") == truth


def test_the_encoding_census_reader_facing_message_carries_the_claim() -> None:
    """INVOKED, not grepped.

    Same reasoning as `test_the_reader_facing_message_carries_the_narrowed_claim`
    above: reading this file for a string spells the needle in the file being
    read, so the assertion always finds ITSELF. Build the message instead and
    assert on what a tripped reader is actually shown.
    """
    msg = encoding_census.findings_message(["tests/test_x.py:12 read_text()"])

    assert encoding_census.UNNAMED_ENCODING_CLAIM in msg, (
        "the message a tripped reader sees no longer carries the shared "
        "claim, so it is a second spelling of it:\n" + msg
    )
    assert "tests/test_x.py:12" in msg, (
        "the message does not name the offending site, so a reader cannot act "
        "on it"
    )
    assert "cp1252" in encoding_census.UNNAMED_ENCODING_CLAIM, (
        "the claim no longer names the encoding that makes this a defect"
    )
    assert "utf-8" in encoding_census.DIRECTIONS, (
        "the directions no longer say what to do"
    )
