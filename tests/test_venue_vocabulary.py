"""The venue vocabulary must survive a rename. A blind find/replace must go RED here.

Why this file exists
--------------------
The repo was renamed `mt5-risk-bot` -> `straightedge` on 2026-09-24. In this tree the
string `mt5` carries FOUR different meanings, and only one of them was the dead product
name:

  1. the PRODUCT name (`mt5_risk_bot`, `mt5-risk-bot`). Dead. Renamed. Must stay at zero.
  2. the MetaTrader 5 VENUE (`mode = "mt5"`, `[mt5]`, `MetaTrader5`, `mt5-win`,
     `mt5-mac`). LIVE. The desk drives MT5 and MT4 both. Must survive byte-for-byte.
  3. the MT4 file-mailbox WIRE CONTRACT (`mt4_risk_bot.req` / `.res`), which spans the
     MQL4 Expert and `broker/mt4_live.py`. Renaming one side and not the other makes the
     Expert write a file the Python side never reads. The desk would just time out.
  4. the id of a LIVE Cloudflare AI Gateway resource that still happens to be called
     `mt5-risk-bot`. Renaming the code reference without renaming the Cloudflare
     resource breaks the agent. That rename needs an infra change request, so the
     literal stays here until the resource itself moves.

A global find/replace over `mt5` would break 2, 3 and 4 while every test that does not
exercise those paths stayed green: MT5 needs a Windows/macOS binding absent from CI, the
MT4 mailbox is faked in the adapter tests, and the gateway is only reached over the
network. That is a false green with real money behind it, which is why this gate counts
the vocabulary directly instead of trusting behaviour to notice.

What this gate does and does not catch
--------------------------------------
It catches DISAPPEARANCE and ARRIVAL: a venue token deleted, mangled or mass-renamed, and
the dead product token coming back. It does not check that any of these sites is
semantically right; `tests/test_mode_gate.py` does that for the mode gate. A count is an
instrument, not a proof. Each expected number below was measured against the tracked tree
at the rename commit. If you intentionally add or remove a venue site, change the
constant in the same commit and say why -- that diff is the review surface.

Proven red: renaming one occurrence of `{"mt5", "mt4"}` in `risk.py` fails
`test_real_money_gate_set_form`, `test_venue_set_form_sites` and
`test_mode_literal_mt5_sites`. A gate seen only green is not a gate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: This file is excluded from its own scan. It quotes every token it pins, including the
#: dead ones, so counting itself would make its own prose load-bearing and would push the
#: dead-module-token count above zero. The exclusion is printed on every run.
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()

# --- expected counts, measured over the TRACKED tree at the rename commit -------------
# Bare `mt5` / `mt4` are too noisy to pin (prose, test names, symbols), so each constant
# pins a token that SELECTS BEHAVIOUR or names a real artifact.

#: `"mt5"` as a quoted literal anywhere under `src/` (mode values, dispatch, argparse
#: choices, the `[mt5]` section lookup key).
MODE_LITERAL_MT5_IN_SRC = 11
#: the MT4 counterpart. The desk drives both venues; a rename that keeps one is broken.
MODE_LITERAL_MT4_IN_SRC = 12
#: the set-form venue test `{"mt5", "mt4"}` under `src/` (risk.py x2, desk.py code +
#: its explaining comment).
VENUE_SET_FORM_IN_SRC = 4
#: `self.cfg.mode in {"mt5", "mt4"}` in `risk.py`. THE REAL-MONEY GATE. Two sites:
#: `send_gate` and `circuit`. If this number drops, a live send stopped being gated.
REAL_MONEY_GATE_IN_RISK = 2
#: the legality check in `config.py`, a different question from "is this a live venue".
LEGAL_MODE_SET_IN_SRC = 1
#: TOML section headers across the tracked tree. The mt4 header is 9, not 2: the two
#: example configs, one inline TOML fixture in tests/test_mt4_adapter.py, the CHANGELOG
#: line naming the `[mt4] startup_wait_sec` key, three inline TOML fixtures in
#: tests/test_mt4_startup_wait.py that pin set / unset / zero for that key, and TWO in
#: tests/test_mt4_net_transport.py. Those last two went 7 -> 9 with the network
#: transport (#73) and they are load-bearing rather than incidental: both write a
#: `[mt4] mailbox_token` into a config FILE and assert the loader does not read it,
#: because that token is environment-only and a TOML key for it would put a
#: trade-placing secret in a file people commit.
#:
#: Both went up by one with the watchdog (issue #38), from ONE line of
#: `docs/RUNBOOK.md` that names which key sets the staleness threshold: the
#: threshold is derived from the venue's own per-command budget, so the operator
#: reading the alarm has to be told which section that budget lives in. It is
#: prose about the key, not a new config site.
CONFIG_SECTION_MT5 = 3
#: 11 after the read/send budget split (#37). The new site is
#: `docs/RUNBOOK.md`, which names `[mt4] send_timeout_ms` beside
#: `[mt4] timeout_ms` to say which of the two the watchdog derives its alarm from.
#: Prose about the pair, not a new config section.
CONFIG_SECTION_MT4 = 11
#: the `[mt5]` section keys, and `timeout_ms` which both venue sections share.
#: timeout_ms went 18 -> 24 with the MT4 startup wait. Only ONE of those six was a new
#: config site (a fixture in tests/test_mt4_startup_wait.py asserting that an absent
#: startup_wait_sec stays unset); the other five are prose ABOUT the steady-state key:
#: both example configs explain that startup_wait_sec is NOT timeout_ms, and docs/MT4.md
#: plus the CHANGELOG state the budget table and the "budget plus one timeout_ms" bound.
#:
#: It went 24 -> 30 with the network transport (#73), and this key is the reason that
#: change is worth reading rather than rubber-stamping: the shim REUSES `timeout_ms` as
#: its own mailbox budget instead of introducing a second number, and the desk allows
#: that plus a 2 second network grace so the shim is the end that gives up first. The
#: six new sites are exactly that statement, in the places an operator or a reviewer
#: looks: one code site (`cmd_mt4_shim` in src/straightedge/__main__.py, which computes
#: the shim's budget from it), two in docs/TRANSPORT.md (the failure table and the
#: budget-ordering paragraph), one in docs/CONTRACT.md, one in the CHANGELOG, and one in
#: tests/test_mt4_net_transport.py. If this number drops back toward 24, the most likely
#: cause is the shim growing a budget of its own, which is the drift this pin catches.
#:
#: It went 30 -> 39 with the watchdog (issue #38), and the reason is the same
#: shape as the shim's: the staleness threshold REUSES this key as the venue
#: term of its budget rather than introducing an alarm window of its own, so
#: every new site is that statement. Attributed by file, because a total nobody
#: can break down is not a measurement: 2 in `src/straightedge/watchdog.py` (the
#: derivation in the module docstring, and the one `getattr` that reads it), 3 in
#: `tests/test_watchdog.py` (the pinned MT4 and MT5 budgets, and the docstring
#: saying that moving this default has to move them), and 1 each in
#: `docs/RUNBOOK.md`, `docs/CONTRACT.md`, `config.example.toml` and the
#: `CHANGELOG.md`. If this number drops toward 30, the most likely cause is the
#: watchdog growing a hardcoded alarm window, which is the drift this pin
#: catches and the exact defect issue #68 measured on the price axis.
KEY_TERMINAL_PATH = 6
#: 39 before the claim-open retry landed, then 41. Neither of those two was a live
#: config read: the 40th is a COMMENT in `mt4/Experts/Mt4RiskBot.mq4` citing
#: `mt4.timeout_ms` to show the arithmetic that bounds the retry against the
#: adapter budget, and the 41st is the same citation in
#: `tests/test_mt4_claim_open_retry.py`, which pins that arithmetic.
#:
#: 122 after the read/send budget split (#37). The jump is large and it is real, and
#: it is largely the SUBSTRING trap this tripwire is built out of: the new key is
#: `send_timeout_ms`, so every one of its sites also matches `timeout_ms`, and a
#: line that names both increments this twice. Attribution by file, counted in the
#: same commit that added them:
#:
#:   23  src/straightedge/config.py            both Mt4Config fields and their
#:                                            reasoning, the loader, and the two
#:                                            new `validate()` refusals
#:   21  tests/test_send_budget.py             the new gate over the derivation
#:   11  tests/test_watchdog.py                unchanged count plus the pin that the
#:                                            alarm derives from the READ budget
#:    7  docs/RUNBOOK.md, docs/MT4.md, config.example.toml, CHANGELOG.md (each)
#:    6  config.handover.toml
#:    5  docs/TRANSPORT.md
#:    4  src/straightedge/watchdog.py, broker/__init__.py, __main__.py (each)
#:    3  tests/test_mt4_net_transport.py, tests/test_mt4_adapter.py,
#:       docs/CONTRACT.md (each)
#:    2  src/straightedge/broker/mt5_live.py
#:    1  tests/test_mt4_startup_wait.py, tests/test_mt4_claim_open_retry.py,
#:       src/straightedge/constants.py, src/straightedge/broker/mt4_live.py,
#:       mt4/Experts/Mt4RiskBot.mq4 (each)
#:
#: There are exactly TWO live config reads across all of it, both in
#: `config.py`'s loader: `timeout_ms` and `send_timeout_ms`. Everything else is a
#: field declaration, a derivation, a test, or prose explaining why the two numbers
#: are not one number.
KEY_TIMEOUT_MS = 122
#: the official Windows pip package, named in the extra, the adapter import, the doctor
#: advice and the mypy override, plus four sites in docs/THIRD-PARTY.md (the licence table,
#: the pip download command, the paragraph under the table, and the MetaQuotes contact note).
METATRADER5 = 25
#: the optional-dependency extras. `mt5-win` is the pyproject extra; `mt5-mac` is both an
#: extra and the macOS package name, so it appears in advice and docs too. docs/THIRD-PARTY.md
#: adds one mt5-win site (its table row) and three mt5-mac sites (two in the row, one in prose).
EXTRA_MT5_WIN = 2
PACKAGE_MT5_MAC = 16
#: the doctor line that reports whether a binding is present at all.
DOCTOR_MT5_BINDING = 3
#: the MT4 file-mailbox basenames. MQL4 Expert and Python adapter must agree exactly.
#: 20 before the mailbox claim landed, plus 9: the Expert's claim-by-rename path and
#: refusal log, the "One Expert, enforced" sections of docs/MT4.md and mt4/README.md,
#: and the CHANGELOG entry that describes the claim.
#: 29 before the claim-open retry landed, then 31, then 32, and none of those three
#: is a new mailbox site: the 30th is the measured EA log line in
#: `tests/test_mt4_claim_open_retry.py` naming `mt4_risk_bot.req.claim.<ChartID>`,
#: the 31st names `mt4_risk_bot.res.tmp` in the same file (the reply staging file
#: whose FileOpen was failing silently), and the 32nd is the `-Base` default in
#: `mt4/tools/measure-mailbox.ps1`, parameterised so the basename appears ONCE
#: there rather than at every filename it builds.
#:
#: 36 after the stale-request fence (#37). Four more, and one of them IS a new file
#: in the mailbox directory:
#:   2  tests/test_mt4_stale_request_fence.py, asserting the Expert stamps
#:      `mt4_risk_bot.req` BEFORE it renames it to the claim path
#:   2  mt4/Experts/Mt4RiskBot.mq4, where the fence reads that stamp and where
#:      `CalibrateFileTime` writes and deletes its own `mt4_risk_bot.timeprobe`
#: The probe is a real new mailbox file. It is created and removed inside the one
#: function that needs it, at OnInit and never again.
MT4_MAILBOX = 36
#: the LIVE Cloudflare AI Gateway id. Deliberately still the old string; see the header.
#: 13 gateway-resource references plus 2 in the RUNBOOK LaunchAgent migration note, plus the
#: four-line vocabulary header of docs/DATA.md, which names the gateway like every other doc.
GATEWAY_ID_AND_MIGRATION_NOTE = 16
#: the dead product module token. Zero, forever.
DEAD_MODULE_TOKEN = 0


def _tracked() -> list[Path]:
    """Every tracked text file, as the thing that actually ships.

    A missing git is a hard failure, never a skip: a vocabulary gate that quietly
    measures nothing is indistinguishable from one that passed.

    **`git ls-files`, so a NEW file is invisible until it is staged**, and that
    reads as a pass rather than as an error. A local run against an untracked
    new test file scans a smaller tree, counts fewer tokens, and goes GREEN on
    constants that CI will then reject; measured on the MT4 startup-wait branch,
    108 files locally against 109 in CI, four tokens apart. Run this gate after
    `git add`, not before. The enumerator is still the right one -- the tracked
    tree IS what ships -- but its denominator is a state the working tree can
    disagree with.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.fail(f"cannot enumerate the tracked tree, so nothing was measured: {exc}")
    names = [n for n in out.stdout.decode().split("\0") if n and n != SELF]
    if not names:  # pragma: no cover
        pytest.fail("the tracked tree enumerated to zero files, so nothing was measured")
    return [ROOT / n for n in names]


def _count(needle: str, under: str = "") -> tuple[int, int]:
    """Return (occurrences, files scanned). Substring count, not line count."""
    total = 0
    scanned = 0
    prefix = f"{under}/" if under else ""
    for path in _tracked():
        rel = path.relative_to(ROOT).as_posix()
        if prefix and not rel.startswith(prefix):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # a binary or unreadable file carries no vocabulary
        scanned += 1
        total += text.count(needle)
    return total, scanned


def _assert(label: str, needle: str, expected: int, under: str = "") -> None:
    got, scanned = _count(needle, under)
    where = under or "the tracked tree"
    where = f"{where} (excluding {SELF})"
    print(f"venue vocabulary: {label}: {got}/{expected} over {scanned} files in {where}")
    assert got == expected, (
        f"{label}: found {got} occurrences of {needle!r} in {where} "
        f"({scanned} files scanned), expected {expected}. "
        "If a venue site was added or removed on purpose, update the constant in this "
        "commit. If this dropped because of a rename, the rename ate a LIVE venue token."
    )


# --- the real-money gate ---------------------------------------------------------------


def test_real_money_gate_set_form() -> None:
    """`risk.py` is what actually gates a send on a real account."""
    risk = (ROOT / "src" / "straightedge" / "risk.py").read_text(encoding="utf-8")
    got = risk.count('self.cfg.mode in {"mt5", "mt4"}')
    print(f"venue vocabulary: real-money gate: {got}/{REAL_MONEY_GATE_IN_RISK} in risk.py")
    assert got == REAL_MONEY_GATE_IN_RISK, (
        f"the real-money gate appears {got} times in risk.py, expected "
        f"{REAL_MONEY_GATE_IN_RISK} (send_gate and circuit). A live send may no longer "
        "be gated on live_accepted."
    )


def test_venue_set_form_sites() -> None:
    _assert("venue set form", '{"mt5", "mt4"}', VENUE_SET_FORM_IN_SRC, "src")


def test_legal_mode_set() -> None:
    _assert("legal mode set", '{"paper", "mt5", "mt4"}', LEGAL_MODE_SET_IN_SRC, "src")


# --- venue mode literals ---------------------------------------------------------------


def test_mode_literal_mt5_sites() -> None:
    _assert("mode literal mt5", '"mt5"', MODE_LITERAL_MT5_IN_SRC, "src")


def test_mode_literal_mt4_sites() -> None:
    _assert("mode literal mt4", '"mt4"', MODE_LITERAL_MT4_IN_SRC, "src")


# --- config vocabulary -----------------------------------------------------------------


def test_config_section_mt5() -> None:
    _assert("config section [mt5]", "[mt5]", CONFIG_SECTION_MT5)


def test_config_section_mt4() -> None:
    _assert("config section [mt4]", "[mt4]", CONFIG_SECTION_MT4)


def test_mt5_section_keys() -> None:
    _assert("key terminal_path", "terminal_path", KEY_TERMINAL_PATH)
    _assert("key timeout_ms", "timeout_ms", KEY_TIMEOUT_MS)


# --- the MT5 binding -------------------------------------------------------------------


def test_metatrader5_package_sites() -> None:
    _assert("MetaTrader5", "MetaTrader5", METATRADER5)


def test_mt5_extras() -> None:
    _assert("extra mt5-win", "mt5-win", EXTRA_MT5_WIN)
    _assert("package mt5-mac", "mt5-mac", PACKAGE_MT5_MAC)


def test_doctor_binding_output() -> None:
    _assert("doctor mt5 binding", "mt5 binding", DOCTOR_MT5_BINDING, "src")


# --- the MT4 mailbox wire contract -----------------------------------------------------


def test_mt4_mailbox_basenames_agree() -> None:
    """One side renamed is a silent timeout, not an error."""
    _assert("mt4 mailbox token", "mt4_risk_bot", MT4_MAILBOX)
    expert = (ROOT / "mt4" / "Experts" / "Mt4RiskBot.mq4").read_text(encoding="utf-8")
    adapter = (ROOT / "src" / "straightedge" / "broker" / "mt4_live.py").read_text(
        encoding="utf-8"
    )
    for name in ("mt4_risk_bot.req", "mt4_risk_bot.res"):
        assert name in expert, f"{name} is gone from the MQL4 Expert"
        assert name in adapter, f"{name} is gone from the Python adapter"


# --- the deliberate residual and the dead token ----------------------------------------


def test_gateway_id_residual_is_deliberate() -> None:
    """The Cloudflare AI Gateway is still named `mt5-risk-bot`. Renaming the code
    reference without renaming the resource breaks the agent, so this literal stays
    until an infra change request moves the resource."""
    _assert("gateway id + migration note", "mt5-risk-bot", GATEWAY_ID_AND_MIGRATION_NOTE)


def test_dead_module_token_stays_dead() -> None:
    _assert("dead module token", "mt5_risk_bot", DEAD_MODULE_TOKEN)


def test_total_pinned_vocabulary() -> None:
    """One printed total, so the denominator is visible in every run."""
    parts = {
        "mode literal mt5 (src)": ('"mt5"', "src"),
        "mode literal mt4 (src)": ('"mt4"', "src"),
        "venue set form (src)": ('{"mt5", "mt4"}', "src"),
        "legal mode set (src)": ('{"paper", "mt5", "mt4"}', "src"),
        "config section [mt5]": ("[mt5]", ""),
        "config section [mt4]": ("[mt4]", ""),
        "terminal_path": ("terminal_path", ""),
        "timeout_ms": ("timeout_ms", ""),
        "MetaTrader5": ("MetaTrader5", ""),
        "mt5-win": ("mt5-win", ""),
        "mt5-mac": ("mt5-mac", ""),
        "doctor mt5 binding (src)": ("mt5 binding", "src"),
        "mt4 mailbox": ("mt4_risk_bot", ""),
    }
    total = 0
    for label, (needle, under) in parts.items():
        got, _ = _count(needle, under)
        total += got
        print(f"venue vocabulary: {label} = {got}")
    expected = (
        MODE_LITERAL_MT5_IN_SRC
        + MODE_LITERAL_MT4_IN_SRC
        + VENUE_SET_FORM_IN_SRC
        + LEGAL_MODE_SET_IN_SRC
        + CONFIG_SECTION_MT5
        + CONFIG_SECTION_MT4
        + KEY_TERMINAL_PATH
        + KEY_TIMEOUT_MS
        + METATRADER5
        + EXTRA_MT5_WIN
        + PACKAGE_MT5_MAC
        + DOCTOR_MT5_BINDING
        + MT4_MAILBOX
    )
    print(f"venue vocabulary: TOTAL pinned venue references = {total}/{expected}")
    assert total == expected
