#!/usr/bin/env python3
"""Census: which test doubles can FAIL the way the real thing does?

The rule this enforces is in `docs/TESTING.md` under "A substitute that cannot
fail the way the real thing does". The short form: a double that only ever
RETURNS cannot enter any failure state, so a suite built on it cannot cover a
failure path through that seam no matter how many cases it has.

This is the measurement, not advice. It was written after straightedge#232,
where 1457 green tests could not see a defect on the error path because every
transport double in the suite returned a payload and a payload cannot raise an
HTTP 502.

The seam names are read from the `Protocol` classes in `src/` rather than
hardcoded, so adding a method to `Broker` or `Transport` widens this census
automatically instead of silently leaving the new seam uncounted.

A double with no `raise` is NOT automatically a defect: most tests exercise a
happy path and should. What the census makes checkable is the CLAIM. If no
double for a seam can raise, then no DOUBLE in this suite covers a failure
path through that seam, and the three honest answers are: make the double
enter that state, have the test disclaim the path, or point at a live run.
Exit codes say which situation you are in; they do not say you are wrong.

WHAT THIS CANNOT SEE, stated because the reading is reassuring either way.
It measures test-file doubles, by looking for a `raise` inside a method whose
name matches a seam. A failure path covered by making the REAL implementation
fail is INVISIBLE to it and reads as blind: `docs/TESTING.md` prefers exactly
that ("when the real thing can be made to fail cheaply, that beats any
double"), and #232's own repair pointed a real transport at a closed loopback
port and needed no double at all. So a blind seam here means "no double can
fail", never "nothing covers the failure"; check for a real-implementation
test before concluding a path is uncovered. Narrowed in #264, where the
previous wording claimed the stronger thing.
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "straightedge"
TESTS = ROOT / "tests"


def protocol_seams() -> dict[str, set[str]]:
    """Method names declared by each `Protocol` class in src/."""
    seams: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(
                isinstance(b, ast.Name) and b.id == "Protocol" for b in node.bases
            ):
                continue
            names = {
                m.name
                for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not m.name.startswith("__")
            }
            if names:
                seams[node.name] = names
    return seams


def can_raise(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Does this method body contain a raise at any depth?

    Deliberately crude: presence, not reachability. A `raise` behind a branch
    the tests never take still means the double was BUILT able to fail, which
    is the distinction being measured. Proving reachability is the test
    author's job and this census does not pretend to do it.
    """
    return any(isinstance(n, ast.Raise) for n in ast.walk(fn))


def collect() -> dict[str, list[tuple[str, str, bool]]]:
    """Seam method -> [(test file, double class, can_raise)].

    The structured half, so a caller can assert on the data instead of on this
    module's exit code or its printed text. Only seams that at least one
    double implements are returned; `main()` prints exactly the same data and
    adds no measurement of its own.

    Raises `RuntimeError` when no `Protocol` class was found, because a census
    that measured nothing must never be readable as "nothing is blind".
    """
    seams = protocol_seams()
    if not seams:
        raise RuntimeError(
            "NOTHING CHECKED: no Protocol classes found in src/. "
            "A census that measured nothing is not a clean result."
        )
    all_methods: set[str] = set()
    for names in seams.values():
        all_methods |= names

    # seam method -> [(file, class, can_raise)]
    found: dict[str, list[tuple[str, str, bool]]] = {m: [] for m in all_methods}
    for path in sorted(TESTS.rglob("*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for member in node.body:
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if member.name in all_methods:
                    found[member.name].append((path.name, node.name, can_raise(member)))
    return {m: rows for m, rows in found.items() if rows}


def blind_seams(
    implemented: dict[str, list[tuple[str, str, bool]]] | None = None,
) -> set[str]:
    """Seams a double implements but where NO double can raise."""
    impl = collect() if implemented is None else implemented
    return {m for m, rows in impl.items() if not any(r[2] for r in rows)}


def main() -> int:
    try:
        seams = protocol_seams()
        found = collect()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 9

    print("Protocol seams read from src/:")
    for cls, names in sorted(seams.items()):
        print(f"  {cls}: {len(names)} methods")
    print()

    implemented = found
    print(f"seam methods implemented by a test double: {len(implemented)}")
    blind: list[str] = []
    for m, rows in sorted(implemented.items()):
        raisers = [r for r in rows if r[2]]
        flag = " " if raisers else "*"
        print(f" {flag} {m:22} doubles={len(rows):3}  can_raise={len(raisers):3}")
        if not raisers:
            blind.append(m)

    print()
    if blind:
        print(f"SEAMS WITH NO FAILING DOUBLE ({len(blind)}):")
        for m in blind:
            where = ", ".join(sorted({f"{f}:{c}" for f, c, _ in found[m]}))
            print(f"  {m}  -- implemented by {where}")
        print()
        print("No double for these seams can raise, so no DOUBLE here covers")
        print("a failure path through them. Check whether a test makes the REAL")
        print("implementation fail, which this census cannot see, before")
        print("reading that as uncovered. If nothing does, that is a finding")
        print("about the CLAIM: make a double enter the state, disclaim the")
        print("path, or cite a live run. See docs/TESTING.md.")
        return 1
    print("every implemented seam has at least one double that can fail")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
