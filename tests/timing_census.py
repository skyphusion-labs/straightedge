#!/usr/bin/env python3
"""Census: which tests assert on ELAPSED WALL-CLOCK TIME?

The population straightedge#258 is about. A test whose correctness requires a
wall-clock budget to be MET is a different kind of test from one whose
correctness is independent of timing, and the suite could not tell them apart,
which is how ~15% of `windows-latest` runs went red on `main` with nothing
wrong. Two more members were added on the evening the population was named, by
an author who flagged them rather than waiting for them to flake; this exists
so the next one cannot arrive undiscovered.

HOW IT DECIDES, and the two ways it was wrong first, because a census is an
instrument and this file is the argument for checking one:

  v1 bound a name only from a DIRECT call (`started = time.monotonic()`), so
  it missed the commonest shape in this suite, `elapsed = time.monotonic() -
  started`, which is a BinOp. It reported 4 members and silently omitted the
  two that prompted it.

  v2 followed derived names to a fixed point and over-collected to 66, because
  `"time"` was matched as a bare attribute name, so `datetime.time` and
  friends tainted a name which then cascaded through the module.

  v3 matched the `time` module exactly but KEPT the transitive closure, and
  still produced four confident false positives in `test_desk.py` -- tests
  about stop placement, reported as elapsed-time assertions, because one
  `time.time()` tainted a chain twenty names long. Module-wide taint has no
  scope.

So the match is now exact: a clock read is `time.monotonic()`,
`time.perf_counter()` or `time.time()` on the `time` MODULE, and only an
ORDERING comparison counts, because `==` on a tainted value is not a budget.
Validated against members known in advance to belong, which is the only way to
tell a census from a number.

The report separates the two directions, because they are not equally fragile:

  CEILING  (elapsed < X)   a stall makes it red. The exposed direction.
  FLOOR    (elapsed >= X)  something must have TAKEN time. Fragile only when
                           the thing that must take time is another thread the
                           test cannot hold still.

A floor the test enforces itself (its own stub, its own window parameter) is
sound; a floor needing another thread to still be holding when this one
arrives is the #258 shape. The census cannot tell those apart -- that reading
is the author's -- so it reports the population and the direction, and the
`timing` mark records the author's answer.
"""

from __future__ import annotations

import ast
import pathlib
import sys

TESTS = pathlib.Path(__file__).resolve().parent
CLOCK_ATTRS = {"monotonic", "perf_counter", "time"}
ORDERING = {"Lt", "LtE", "Gt", "GtE"}


def _is_clock_call(node: ast.AST) -> bool:
    """`time.monotonic()` and friends, on the `time` module specifically."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return (
        isinstance(f, ast.Attribute)
        and f.attr in CLOCK_ATTRS
        and isinstance(f.value, ast.Name)
        and f.value.id == "time"
    )


def clock_names(tree: ast.Module) -> set[str]:
    """Locals assigned from an expression that DIRECTLY reads a clock.

    One hop, deliberately, and no transitive closure. v3 took the transitive
    closure over the whole module and cascaded: one `expires = time.time() +
    ttl` tainted `pending`, then `spec`, `sl`, `tp`, `tick` and twenty more in
    `test_desk.py`, which produced four confident false positives in tests
    about stop placement. Module-wide taint has no scope, so anything assigned
    from anything tainted joins, and the closure eats the file.

    One hop is enough for the real population, which is why the closure was
    never needed: both shapes that matter carry the call in the expression
    itself.

        started = time.monotonic()                 # direct
        elapsed = time.monotonic() - started       # direct, inside a BinOp

    Checked against members known in advance to belong (the two
    concurrent-reader cases #258 was asked to cover) and against a known
    NON-member (`test_desk.py`, which must contribute nothing).
    """
    out: set[str] = set()
    for n in ast.walk(tree):
        value = n.value if isinstance(n, (ast.Assign, ast.AnnAssign)) else None
        if value is None:
            continue
        if not any(_is_clock_call(x) for x in ast.walk(value)):
            continue
        targets = n.targets if isinstance(n, ast.Assign) else [n.target]
        for t in targets:
            if isinstance(t, ast.Name):
                out.add(t.id)
    return out


def collect() -> list[tuple[str, str, str, bool]]:
    """Every elapsed-time assertion, as (file, test, direction, marked).

    The structured half, so a test can assert on the data instead of on this
    module's exit code. `main()` prints the same rows and adds nothing.

    Raises `RuntimeError` when there are no test files to read, because a
    census that measured nothing is not a clean result and a caller must not
    be able to read that as an empty population.
    """
    files = sorted(TESTS.rglob("test_*.py"))
    if not files:
        raise RuntimeError("NOTHING CHECKED: no test files found")

    rows: list[tuple[str, str, str, bool]] = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        bound = clock_names(tree)
        if not bound:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("test_"):
                continue
            marked = any(
                isinstance(d, ast.Attribute) and d.attr == "timing"
                for d in fn.decorator_list
            )
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assert):
                    continue
                if not isinstance(node.test, ast.Compare):
                    continue
                ops = {type(o).__name__ for o in node.test.ops}
                if not (ops & ORDERING):
                    continue
                names = {
                    n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)
                }
                if not (names & bound) and not any(
                    _is_clock_call(n) for n in ast.walk(node.test)
                ):
                    continue
                low = bool(ops & {"Gt", "GtE"})
                high = bool(ops & {"Lt", "LtE"})
                kind = "BOTH" if low and high else ("FLOOR" if low else "CEILING")
                rows.append((path.name, fn.name, kind, marked))
                break

    return rows


def main() -> int:
    try:
        rows = collect()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 9

    print(f"tests asserting on elapsed wall-clock time: {len(rows)}")
    unmarked = []
    for f, fn, kind, marked in sorted(rows):
        print(f" {' ' if marked else '*'} {kind:<8} {f}::{fn}")
        if not marked:
            unmarked.append((f, fn))

    print()
    if unmarked:
        print(f"NOT MARKED `timing` ({len(unmarked)}):")
        for f, fn in unmarked:
            print(f"  {f}::{fn}")
        print()
        print("Each asserts on a wall-clock budget without saying so. Either")
        print("mark it, or make the assertion independent of timing the way")
        print("#258 did for the ttl case.")
        return 1
    print("every elapsed-time assertion is marked `timing`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
