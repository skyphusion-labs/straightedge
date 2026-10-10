"""pytest wrapper so the release path is exercised on EVERY pull request.

straightedge#259. The assembler runs once per release, which is exactly long
enough to rot unnoticed between uses, so its guards cannot live in a runbook
step that nobody executes until the day it matters. `testpaths = ["tests"]`
means these run in `ci-matrix` on every PR, on both ubuntu and windows and on
both supported Pythons.

This file deliberately does NOT re-implement the checks: it invokes the shipped
self-test and the shipped `--check`, so what CI exercises is the code a release
runs rather than a copy of it that can agree with a broken original. That is the
rebuilt-consumer defect from `docs/TESTING.md`, and a test suite is the easiest
place to commit it.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
TOOL = REPO / "tests" / "changelog_assemble.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )


def test_assembler_self_test_passes_and_reports_its_case_count() -> None:
    """The tool's own controls, driven through the real entry point.

    Asserting on the printed case COUNT as well as the status, because a
    self-test that silently stopped running cases would still exit 0.
    """
    out = run("--self-test")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "0 failure(s)" in out.stdout, out.stdout
    cases = [l for l in out.stdout.splitlines() if l.startswith("  ok ")]
    assert len(cases) >= 18, "self-test ran only %d cases:\n%s" % (len(cases), out.stdout)


def test_the_repository_itself_satisfies_the_one_place_invariant() -> None:
    """`--check` against the real tree, which is the state a release starts from.

    This is the assertion that keeps the convention in force: it fails if anyone
    writes an entry straight into `CHANGELOG.md` under `## Unreleased`, which is
    the second home that made the old convention unenforceable.
    """
    out = run("--check")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "VERDICT: ok" in out.stdout, out.stdout


def test_check_fails_when_an_entry_is_written_straight_into_the_changelog(tmp_path) -> None:
    """The positive control for the test above.

    Without this, `--check` passing on a clean tree proves only that the tree is
    clean, never that the check can see the defect.
    """
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "1-a-fragment.md").write_text("### A fragment\n\n- x\n")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n### A stray entry\n\n- x\n\n## 1.0.0\n\n### Old\n"
    )
    out = run("--check", "--root", str(tmp_path))
    assert out.returncode == 1, out.stdout
    assert "two homes" in out.stdout, out.stdout


def test_a_release_refuses_to_assemble_a_malformed_fragment(tmp_path) -> None:
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "Bad_Name.md").write_text("### x\n\n- y\n")
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n\n## 1.0.0\n\n### Old\n")
    out = run("--version", "9.9.9", "--root", str(tmp_path))
    assert out.returncode == 1, out.stdout
    assert "does not match" in out.stdout, out.stdout


def test_a_release_assembles_in_issue_order_and_clears_the_directory(tmp_path) -> None:
    """The mutation, end to end, including that the fragments are removed.

    Ordering is checked with 9 and 217 present, because a lexical sort puts
    "217" before "9" and that is the bug a reader would never notice in a
    rendered changelog.
    """
    d = tmp_path / "changelog.d"
    d.mkdir()
    (d / "217-later-issue.md").write_text("### Later issue (issue #217)\n\n- later\n")
    (d / "9-earlier-issue.md").write_text("### Earlier issue (issue #9)\n\n- earlier\n")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n## 1.0.0\n\n### Old entry\n\n- old\n"
    )

    out = run("--version", "1.1.0", "--apply", "--root", str(tmp_path))
    assert out.returncode == 0, out.stdout + out.stderr

    text = (tmp_path / "CHANGELOG.md").read_text()
    assert "## 1.1.0" in text
    assert "## Unreleased" in text, "the empty Unreleased heading must survive"
    assert "### Old entry" in text, "the released section must be untouched"
    assert text.index("Earlier issue") < text.index("Later issue"), text
    assert text.index("## 1.1.0") < text.index("## 1.0.0"), "new version must be above the old"
    assert list(d.glob("*.md")) == [], "fragments must be cleared by a release"


def test_a_dry_run_changes_nothing(tmp_path) -> None:
    d = tmp_path / "changelog.d"
    d.mkdir()
    (d / "1-only.md").write_text("### Only (issue #1)\n\n- x\n")
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("# Changelog\n\n## Unreleased\n\n## 1.0.0\n\n### Old\n")
    before = cl.read_text()

    out = run("--version", "2.0.0", "--root", str(tmp_path))
    assert out.returncode == 0, out.stdout
    assert cl.read_text() == before, "a dry run must not write"
    assert list(d.glob("*.md")) != [], "a dry run must not delete"
    assert "## 2.0.0" in out.stdout, "a dry run must SHOW what it would write"


@pytest.mark.parametrize("name", ["0000-no-issue.md", "1-x.md", "123456-a-b-c.md"])
def test_accepted_names(tmp_path, name: str) -> None:
    d = tmp_path / "changelog.d"
    d.mkdir()
    (d / name).write_text("### Heading\n\n- x\n")
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n\n## 1.0.0\n\n### Old\n")
    out = run("--check", "--root", str(tmp_path))
    assert out.returncode == 0, out.stdout
