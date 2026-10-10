#!/usr/bin/env python3
"""Census: does every text-mode file operation name its encoding?

`open()`, `Path.read_text()` and `Path.write_text()` in TEXT mode decode and
encode with `locale.getpreferredencoding(False)` when no `encoding=` is given.
On the `windows-latest` leg of the CI matrix that is **cp1252**, not utf-8, and
this project writes every file it owns as utf-8 (`journal.py`, `inflight.py`,
`state.py` and `telegram.py` all pass `encoding="utf-8"` explicitly). So an
encoding-less read of our own output is a platform-dependent misread.

It fails in TWO ways and the second is worse:

  RAISE   a byte undefined in cp1252 raises `UnicodeDecodeError` naming an
          offset rather than a cause. 0x81 is such a byte, and it is already
          in this repo: `tests/test_advice_symbol_is_not_manufactured.py`
          carries it as the third byte of U+FB01, from #197's corpus.
  MISREAD every byte of a utf-8-encoded Latin-1 character IS defined in
          cp1252, so a file holding a Latin-1 accented word comes back
          with each of its characters split into two. Nothing reports
          this; the assertion just compares the wrong strings. The test
          named below pins the exact strings, using escapes so this
          instrument stays pure ASCII.

`tests/test_the_censuses_have_an_invoker.py` asserts both halves against
synthesized bytes, so the reason this census exists cannot rot into folklore.

WHY A CENSUS AND NOT A LINT RULE. Measured on ruff 0.17.0 against this repo
before this census was written: `--preview --select PLW1514`
(`unspecified-encoding`, the rule for exactly this defect) found **6** of the
**40** sites that were here. It fires only where it can infer the receiver is a
`pathlib.Path`, which in practice means a literal `Path(...)` inside the same
expression. It was blind to `path.read_text()` on an annotated parameter, to
`(tmp_path / "j.jsonl").read_text()` on a fixture, and to every `write_text`
site -- including the one at
`test_the_replace_idiom_is_guarded_everywhere.py:614`, where an encoding-less
read was serving as an `assert` FAILURE MESSAGE, so the diagnostic would raise
instead of printing on the one platform where it mattered. A rule that covers
15% of the population and reports clean is the shape `docs/TESTING.md` is
mostly about. It is also preview-only on this ruff, which would opt a floating,
dependabot-bumped `ruff>=` dependency into unstable rules. This census keys on
the METHOD NAME, so the receiver's type is never load-bearing.

WHY IT IS COLLECTED AS A TEST rather than a CI step: see
`tests/test_the_censuses_have_an_invoker.py`, which argues it for
`double_census` and `timing_census` and applies here unchanged. The suite is
already a required context, so this gates for free under
`strict_required_status_checks_policy: true`.

WHY CLASSIFICATION IS BY SIGNATURE POSITION AND NOT BY LOOKING FOR A MODE
STRING. Recorded because the first version of this file got it wrong and the
positive control is what caught it. That version called any string argument
whose characters were all in `rwxabt+U` the mode. `open("x", "rb")` therefore
read its FILENAME as the mode, decided it was text, and reported it as a
finding; `write_text("ab")` was read as binary and skipped entirely, a false
NEGATIVE in the one direction that matters. `read_text` and `write_text` have
no mode parameter at all, and `encoding` is positional on all four callees, so
the only correct reading is by index into each one's real signature.

WHAT THIS CANNOT SEE, stated because the reading is reassuring either way. It
measures the CALL, not the file: a site that correctly names `encoding="utf-8"`
while the bytes on disk are cp1252 is invisible here and is a different
defect. It does not follow aliases, so `r = p.read_text` then `r()` reads as no
call site at all. It treats a forwarded `**kwargs` as naming an encoding,
because it cannot see into the dict and a finding it cannot substantiate is
worse than none. And it says nothing about `read_bytes`/`write_bytes`, which
are correct by construction because they never decode.

UNRESOLVED IS A FAILURE, NOT A SKIP. A scanner that quietly passes over what it
cannot classify stops being a denominator, which is the #61 defect. Every
candidate lands in exactly one bucket, and the unclassifiable bucket exits
non-zero with the site named.

Run it directly for the directions:

    python3 tests/encoding_census.py
"""

from __future__ import annotations

import ast
import pathlib
import sys
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Scanned roots. `deploy` is here and not just `src`+`tests` (the
#: `ruff check` scope) because `deploy/windows/assert-config-loads.py` is the
#: one file in the repo that runs ONLY on the cp1252 platform.
SCAN_ROOTS = ("src", "tests", "deploy")

#: Real signatures, as (mode index, encoding index). `None` means the callee
#: has no such parameter. These are the positions CPython actually uses:
#:
#:   open(file, mode="r", buffering=-1, encoding=None, ...)
#:   Path.open(mode="r", buffering=-1, encoding=None, ...)
#:   Path.read_text(encoding=None, errors=None, newline=None)
#:   Path.write_text(data, encoding=None, errors=None, newline=None)
#:
#: `read_text` and `write_text` are ALWAYS text mode: there is no mode
#: parameter to put a "b" in.
SIGNATURES = {
    "builtin_open": (1, 3),
    "path_open": (0, 2),
    "read_text": (None, 0),
    "write_text": (None, 1),
}

TEXT_METHODS = frozenset({"read_text", "write_text"})
OPEN_NAME = "open"

#: Receivers whose `.open()` takes NO `encoding` parameter, so naming one is a
#: TypeError rather than a fix. Exempt by API, never by preference.
#:
#: `os.open` returns a raw file descriptor. `os.fdopen` is deliberately NOT
#: here: it DOES take `encoding`, and `src/straightedge/state.py` passes it.
NO_ENCODING_PARAM = frozenset({"os"})

#: THE CLAIM, in one place, because the reader-facing message and the
#: assertion message are the two spellings that actually reach a person, and
#: `double_census.BLIND_SEAM_CLAIM` exists for the same reason.
UNNAMED_ENCODING_CLAIM = (
    "These text-mode file operations decode or encode through the platform "
    "locale, which is cp1252 on the windows-latest leg and utf-8 everywhere "
    "else, so they read this project's own utf-8 output differently per "
    "platform."
)

DIRECTIONS = (
    'Add encoding="utf-8" to each site. That is the right answer for every '
    "file this project writes, because every writer in src/ already passes "
    "it. If a site genuinely must follow the platform, say so with "
    'encoding="locale", which names an encoding and clears this census '
    "deliberately rather than by omission. If the data is binary, use "
    'read_bytes / write_bytes, or mode="rb"/"wb".'
)


@dataclass
class Census:
    """Every candidate call site, in exactly one bucket."""

    files_scanned: int = 0
    #: Sites that name an encoding, positionally or by keyword. Correct.
    named: list[str] = field(default_factory=list)
    #: Binary mode: nothing is decoded, so no encoding applies.
    binary: list[str] = field(default_factory=list)
    #: Exempt because the callee has no `encoding` parameter at all.
    exempt_api: list[str] = field(default_factory=list)
    #: THE FINDINGS: text mode, no encoding named.
    findings: list[str] = field(default_factory=list)
    #: Could not be classified. A failure, never a skip.
    unresolved: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            len(self.named)
            + len(self.binary)
            + len(self.exempt_api)
            + len(self.findings)
            + len(self.unresolved)
        )


def _scanned_files(
    roots: list[pathlib.Path] | None = None,
) -> list[pathlib.Path]:
    """The denominator. `roots` is for the positive control ONLY.

    The control has to prove this scanner can FIND an offender, and the only
    honest way to show that is to hand it one. Pointing it at a synthesized
    tree beats editing a real file under `src/`, which is the shape of control
    that never gets run.
    """
    out: list[pathlib.Path] = []
    chosen = roots if roots is not None else [ROOT / n for n in SCAN_ROOTS]
    for root in chosen:
        if root.is_dir():
            out.extend(sorted(root.rglob("*.py")))
        elif root.is_file() and root.suffix == ".py":
            out.append(root)
    return out


def _imported_names(tree: ast.Module) -> dict[str, str]:
    """Local name -> module it came from, for resolving a bare or module `open`."""
    table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                table[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                table[a.asname or a.name] = node.module
    return table


def _arg_at(call: ast.Call, index: int | None, name: str) -> ast.expr | None:
    """The argument at a signature position, whether positional or keyword."""
    if index is not None and len(call.args) > index:
        return call.args[index]
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _forwards_kwargs(call: ast.Call) -> bool:
    return any(kw.arg is None for kw in call.keywords)


def scan(roots: list[pathlib.Path] | None = None) -> Census:
    """Classify every text-IO candidate under `roots` (default `SCAN_ROOTS`).

    Raises `RuntimeError` when no file was scanned, because a census that
    measured nothing must never be readable as "nothing is wrong".
    """
    c = Census()
    files = _scanned_files(roots)
    if not files:
        raise RuntimeError(
            "NOTHING CHECKED: no .py files found under %s. A census that "
            "measured nothing is not a clean result."
            % ", ".join(str(r) for r in (roots or SCAN_ROOTS))
        )
    for path in files:
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            c.unresolved.append("%s: does not parse: %s" % (path, exc))
            continue
        c.files_scanned += 1
        try:
            rel = path.relative_to(ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()  # a synthesized tree from the control
        imports = _imported_names(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = isinstance(func, ast.Attribute)
            name = func.attr if attr else getattr(func, "id", None)
            if name not in TEXT_METHODS and name != OPEN_NAME:
                continue

            where = "%s:%d" % (rel, node.lineno)

            # --- which signature are we looking at? --------------------
            if name in TEXT_METHODS:
                kind: str | None = name
            elif not attr:
                # A BARE `open`: the builtin, unless an import shadowed it.
                origin = imports.get(OPEN_NAME)
                if origin is not None:
                    c.unresolved.append(
                        "%s bare open() shadowed by an import from %r, whose "
                        "signature this census does not know" % (where, origin)
                    )
                    continue
                kind = "builtin_open"
            else:
                recv = func.value
                recv_src = ast.get_source_segment(text, recv) or "?"
                if recv_src in NO_ENCODING_PARAM:
                    c.exempt_api.append(
                        "%s %s.open (no encoding parameter)" % (where, recv_src)
                    )
                    continue
                # A module's `open` is not `Path.open` and its signature may
                # differ (gzip, codecs, io, tarfile...). Resolve through the
                # import table rather than assuming Path.
                if isinstance(recv, ast.Name) and recv.id in imports:
                    c.unresolved.append(
                        "%s %s.open() from module %r, whose signature this "
                        "census does not know" % (where, recv.id, imports[recv.id])
                    )
                    continue
                kind = "path_open"

            mode_idx, enc_idx = SIGNATURES[kind]

            # --- binary or text? ---------------------------------------
            if mode_idx is not None:
                mode = _arg_at(node, mode_idx, "mode")
                if mode is not None:
                    if not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
                        c.unresolved.append(
                            "%s %s() with a non-literal mode, so text-versus-"
                            "binary cannot be decided here" % (where, name)
                        )
                        continue
                    if "b" in mode.value:
                        c.binary.append("%s %s(mode=%r)" % (where, name, mode.value))
                        continue

            # --- is an encoding named? ---------------------------------
            if _arg_at(node, enc_idx, "encoding") is not None:
                c.named.append("%s %s()" % (where, name))
                continue
            if _forwards_kwargs(node):
                c.named.append("%s %s() forwarding **kwargs" % (where, name))
                continue
            c.findings.append("%s %s() with no encoding named" % (where, name))
    return c


def collect(roots: list[pathlib.Path] | None = None) -> Census:
    """The structured half, so a caller asserts on data not on exit codes."""
    return scan(roots)


def findings_message(findings: list[str]) -> str:
    """The text a reader is shown when this gate trips.

    A FUNCTION so the property "this message carries the claim and names the
    sites" can be asserted by CALLING it, rather than by grepping this file
    for a string that would always find itself.
    """
    return "%d text-mode file operation(s) do not name an encoding:\n  %s\n\n%s\n\n%s" % (
        len(findings),
        "\n  ".join(findings),
        UNNAMED_ENCODING_CLAIM,
        DIRECTIONS,
    )


def main() -> int:
    try:
        c = scan()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 9

    print("denominator: %d .py files under %s" % (c.files_scanned, ", ".join(SCAN_ROOTS)))
    print("text-IO call sites classified: %d" % c.total)
    print("  named an encoding   %4d" % len(c.named))
    print("  binary mode         %4d" % len(c.binary))
    print("  exempt by API       %4d" % len(c.exempt_api))
    print("  NO ENCODING         %4d" % len(c.findings))
    print("  unresolved          %4d" % len(c.unresolved))
    print()

    if c.unresolved:
        print(
            "UNRESOLVED (%d) -- this census could not classify these, which is "
            "a finding about the SCANNER, not a clean result:" % len(c.unresolved)
        )
        for u in c.unresolved:
            print("  %s" % u)
        print()
        return 2

    if c.findings:
        print(findings_message(c.findings))
        return 1

    print("every text-mode file operation names an encoding")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
