"""Census every filesystem replace/rename/move CALL SITE in the package (#251).

`write tmp, chmod, replace` is a convention repeated across this package, and
the thing that keeps going wrong with it is not the loop: it is the SCANNER.

A regex for `os\\.replace` is blind to `tmp.replace(dest)`, which is the form
most of these sites use, and that blindness is recorded on #251 itself. So this
reads the module as a TREE, the way `refusal_scan.py` does for refusal reasons,
and for the same reason.

The harder half, learned from `refusal_scan.py`: **a site this scanner cannot
classify is a FINDING, not a skip.** `replace` is four different functions
wearing one name in this codebase -- `os.replace`, `Path.replace`,
`str.replace`, `datetime.replace` and `dataclasses.replace` -- and a classifier
that guesses will mis-sort in BOTH directions. Measured while writing this:
a 1-positional-argument heuristic sorted `replace(cfg, mode="mt5")`
(`dataclasses.replace`) into the filesystem bucket at four sites. A scanner that
silently dropped those would have been the regex defect in a more sophisticated
costume, with the sign flipped.

Sites are keyed by (module, enclosing function) and NEVER by line number, so
the registry below does not rot the moment anything above a call site moves.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

#: The helper every guarded site must go through.
GUARD = "replace_retrying_on_share_conflict"

#: Names that MIGHT be a filesystem move. Every one of them is classified.
_CANDIDATES = {"replace", "rename", "move"}

#: The only two sites allowed to call a bare `os.replace`, each with its reason.
#: Pinned as an exact set rather than an ignore list: a third one has to be
#: looked at by a person, which is the whole point of #251.
ALLOWED_BARE = {
    ("atomic.py", "replace_retrying_on_share_conflict"): (
        "IS the guard; the one place the real syscall is allowed to be bare"
    ),
    ("broker/mt4_live.py", "_atomic_write"): (
        "keeps its OWN retry loop on purpose: the mailbox is the interface a "
        "customer installs against, so it is the most expensive thing here to "
        "change and it was already correct (atomic.py says this too)"
    ),
}


@dataclass
class Census:
    #: Bare filesystem replaces outside ALLOWED_BARE. Must be empty.
    findings: list[str] = field(default_factory=list)
    #: Sites this scanner could not classify. Must be empty: see the docstring.
    unresolved: list[str] = field(default_factory=list)
    #: Sites that go through the guard.
    guarded: list[str] = field(default_factory=list)
    #: Bare sites that ARE in ALLOWED_BARE, returned so the caller pins them.
    allowed_bare: list[str] = field(default_factory=list)
    #: Classified as not-a-filesystem-move, returned so the caller can see the
    #: denominator rather than trust a silent exclusion.
    non_fs: list[str] = field(default_factory=list)
    files_scanned: int = 0


def _function_ranges(tree: ast.AST) -> list[tuple[int, int, str]]:
    """Every function's (start, end, name), computed ONCE per file.

    The first version of this walked the whole tree for every candidate call
    site, which made the four census tests take 15 seconds EACH on `engine.py`
    alone. The scan is run per test, so that was a minute of CI for a lookup
    that is a sorted-range search.
    """
    out: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", None) or node.lineno
            out.append((node.lineno, end, node.name))
    out.sort()
    return out


def _enclosing_at(ranges: list[tuple[int, int, str]], lineno: int) -> str:
    """Innermost function containing `lineno`, or "<module>"."""
    best = "<module>"
    for start, end, name in ranges:
        if start > lineno:
            break
        if lineno <= end:
            best = name
    return best


def _imported_names(tree: ast.AST) -> dict[str, str]:
    """Bare name -> module it came from, for `from X import replace` forms."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                out[alias.asname or alias.name] = node.module
    return out


@lru_cache(maxsize=8)
def _scan_cached(root_str: str) -> Census:
    return _scan(Path(root_str))


def scan(root: Path) -> Census:
    """Cached by root: four tests assert on this and each one re-parsed 29 files."""
    return _scan_cached(str(root))


def _scan(root: Path) -> Census:
    pkg = Path(root) / "src" / "straightedge"
    c = Census()
    for f in sorted(pkg.rglob("*.py")):
        # POSIX SEPARATORS ALWAYS. `str(relative_to(...))` renders
        # `broker\mt4_live.py` on Windows, which does not match the
        # forward-slash keys in ALLOWED_BARE, so the mailbox site fell out of
        # the allow-list and into `findings`. The ubuntu legs cannot see that
        # and the windows-latest leg caught it: 2 failed, 1570 passed. The keys
        # are a contract written by a person, so the scanner normalises to them
        # rather than the keys bending to the platform.
        rel = f.relative_to(pkg).as_posix()
        text = f.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(f))
        imports = _imported_names(tree)
        ranges = _function_ranges(tree)
        c.files_scanned += 1
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            enc = _enclosing_at(ranges, node.lineno)
            where = f"{rel}:{enc}"
            if isinstance(fn, ast.Name) and fn.id == GUARD:
                c.guarded.append(where)
                continue
            if isinstance(fn, ast.Attribute) and fn.attr == GUARD:
                c.guarded.append(where)
                continue
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name not in _CANDIDATES:
                continue

            npos, nkw = len(node.args), len(node.keywords)
            if isinstance(fn, ast.Name):
                # A BARE name: only resolvable through the import table.
                origin = imports.get(fn.id, "")
                if origin == "dataclasses":
                    c.non_fs.append(f"{where} dataclasses.{name}")
                elif origin in {"os", "shutil"}:
                    c.findings.append(f"{where} bare {origin}.{name}")
                else:
                    c.unresolved.append(
                        f"{where} bare {name}() from an unknown module {origin!r}"
                    )
                continue

            recv = ast.get_source_segment(text, fn.value) or "?"
            if recv in {"os", "shutil"}:
                key = (rel, enc)
                entry = f"{where} {recv}.{name}"
                (c.allowed_bare if key in ALLOWED_BARE else c.findings).append(entry)
                continue
            if name == "move":
                c.unresolved.append(f"{where} .move() on {recv!r}")
                continue
            if npos >= 2 and nkw == 0:
                # str.replace(old, new); Path.replace takes exactly one.
                c.non_fs.append(f"{where} str.{name}({recv})")
                continue
            if npos == 0 and nkw >= 1:
                # datetime.replace(tzinfo=...) and friends.
                c.non_fs.append(f"{where} {name}(**kw) on {recv!r}")
                continue
            if npos == 1 and nkw == 0:
                # Path.replace(target): a filesystem move.
                key = (rel, enc)
                entry = f"{where} {recv}.{name}(target)"
                (c.allowed_bare if key in ALLOWED_BARE else c.findings).append(entry)
                continue
            c.unresolved.append(
                f"{where} {name}() with {npos} positional and {nkw} keyword args"
            )
    return c
