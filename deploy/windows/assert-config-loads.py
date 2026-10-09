"""Does this config file load, and what does the desk derive from it.

Run this BEFORE restarting a desk, and run it instead of reading the file back.

Why reading the file back is not the check. Measured live on 2026-10-08, on a
real-money box: an edit made with Windows PowerShell 5.1's
`Set-Content -Encoding UTF8` wrote a UTF-8 BOM. The file looked perfect in any
editor and `Get-Content` returned exactly the expected text, because the BOM is
invisible to both. `tomllib` refused it with "Invalid statement (at line 1,
column 1)" and the desk would not have started at its next restart. The edit
was reverted from a backup before that happened.

So the only check worth running is the one the DESK runs: the loader. That is
what this script is. `src/straightedge/config.py` now also tolerates a BOM and
says so on stderr, which means this script reports the warning too rather than
being the only thing standing between a careless cmdlet and an outage.

NO SIDE EFFECTS, by construction. It loads a config and prints derived numbers.
It does not touch the venue, Telegram, the journal, the run lock or the
heartbeat, so it is safe against a live autonomous desk at any time. Exit 0
means the desk can read this file; exit 2 means it cannot, and the reason is on
stderr.

Usage:
    python deploy\\windows\\assert-config-loads.py C:\\bot\\config.toml
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    target = Path(args[0])
    if not target.is_file():
        print(f"no such config file: {target}", file=sys.stderr)
        return 2

    # Imported here rather than at module scope so a missing or broken install
    # reports as its own failure with a usable message, instead of as an
    # ImportError traceback during a deploy.
    try:
        from straightedge import watchdog
        from straightedge.config import load_config
    except Exception as exc:  # pragma: no cover - exercised by hand, not in CI
        print(f"straightedge is not importable: {exc}", file=sys.stderr)
        print("run `pip install -e .` in the checkout before deploying", file=sys.stderr)
        return 2

    try:
        cfg = load_config(target)
    except Exception as exc:
        # The loader's own exception, verbatim. A BOM reports as
        # "Invalid statement (at line 1, column 1)" from tomllib, which names
        # neither the cause nor the remedy, so the hint follows it.
        print(f"config did NOT load: {type(exc).__name__}: {exc}", file=sys.stderr)
        raw = target.read_bytes()[:3]
        if raw == b"\xef\xbb\xbf":
            print(
                "this file starts with a UTF-8 BOM. Re-save it without one: "
                "[System.IO.File]::WriteAllText($p, $text, "
                "(New-Object System.Text.UTF8Encoding $false))",
                file=sys.stderr,
            )
        return 2

    # The figures a deploy and the supervision audit both need, from the one
    # place that derives them. Printed rather than asserted: this script says
    # what the desk will believe, and a human compares it to what they intended.
    stale = watchdog.stale_after_seconds(cfg)
    print(f"config loaded      : {target}")
    print(f"mode               : {cfg.mode}")
    print(f"journal_path       : {cfg.journal_path}")
    print(f"poll_seconds       : {cfg.poll_seconds}")
    print(f"tick_budget_s      : {watchdog.tick_budget_seconds(cfg)}")
    print(f"stale_after_s      : {stale}")
    print(f"restart interval   : must be at or under {stale}s (see deploy/windows/README.md)")
    print(f"telegram enabled   : {bool(cfg.telegram.enabled)}")
    # NAMES ONLY. notify_events is the list that decides whether a restart
    # announces itself at all, and `start` being in it is load-bearing, but
    # nothing else from this file is printed: it carries credentials.
    print(f"notify_events      : {', '.join(cfg.telegram.notify_events)}")
    print(f"start is notified  : {'start' in cfg.telegram.notify_events}")
    if not cfg.telegram.enabled:
        print(
            "NOTE: telegram is not configured in this file, so stale_after_s "
            "above is missing its long-poll term and is not the figure a "
            "running desk gets. `run` refuses to start without telegram.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
