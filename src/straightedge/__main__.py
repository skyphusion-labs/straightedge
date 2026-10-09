"""CLI: paper, backtest, live, doctor.

Live real-money trading requires --i-accept-risk. Demo accounts do not.
Nothing here guarantees profit.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from straightedge import __version__
from straightedge.broker import broker_for
from straightedge.broker.base import venue_clock_of
from straightedge.broker.mt4_net import (
    DEFAULT_SHIM_PORT,
    TOKEN_ENV,
    make_shim,
    serve,
)
from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig, load_config
from straightedge.engine import Engine, run_backtest
from straightedge.history import preflight
from straightedge.journal import (
    InstanceLock,
    InstanceLockError,
    mask_account_id,
    redact_text,
)
from straightedge.models import Bar
from straightedge.strategy import TrendStrategy
from straightedge import supervision
from straightedge.synthetic import generate_bars, generate_ranging
from straightedge.telegram import TelegramClient, TgCommand, offset_path_for
from straightedge.watchdog import (
    heartbeat_path_for,
    stale_after_seconds,
    tick_budget_seconds,
    watch,
)


def _cfg(args: argparse.Namespace) -> BotConfig:
    cfg = load_config(args.config) if args.config else load_config()
    if getattr(args, "i_accept_risk", False):
        cfg.live_accepted = True
    cfg.validate()
    return cfg


def _posture_line(cfg: BotConfig) -> str:
    """Operator-visible handover posture (#25): is either unattended-send
    path available on THIS config, without reading config.toml by hand."""
    approve = "allowed" if cfg.telegram.allow_approve_always else "disabled"
    auto = "allowed" if cfg.telegram.allow_auto else "disabled"
    return f"approve always: {approve}\nauto: {auto}"


def _settings_source_line(cfg: BotConfig) -> str:
    """Which secrets came from `config.toml` rather than the environment.

    Key NAMES, never values, exactly as `mt4_transport_line` presence-checks
    its token without printing it. SECURITY.md tells the operator to keep
    secrets in the environment and the loader accepts the file as a fallback
    (straightedge#139), so the one thing an operator cannot otherwise learn
    without opening a 0600 file by hand is which way THIS config went.

    The empty case is printed, not skipped. A line that appears only when
    something is wrong is indistinguishable from a line nobody implemented,
    and `doctor` already refuses that trade elsewhere ("history: NOT MEASURED").
    """
    lines = []
    if cfg.settings_from_file:
        names = ", ".join(cfg.settings_from_file)
        lines.append(
            f"secrets: {len(cfg.settings_from_file)} from config.toml ({names}); "
            "the environment overrides any of them. Values are never printed"
        )
    else:
        lines.append("secrets: all from the environment")
    if cfg.settings_read_from_nowhere:
        # Louder than the line above, because this one is an operator
        # instruction being discarded rather than a supported fallback.
        names = ", ".join(cfg.settings_read_from_nowhere)
        lines.append(
            f"secrets IGNORED in config.toml: {names}. The loader reads these "
            "from nowhere; put the value in the environment variable instead, "
            "and remove it from the file"
        )
    return "\n".join(lines)


def _presence(cfg: BotConfig, toml_key: str, value: object) -> str:
    """`SET (env)`, `SET (config.toml)`, or `unset`. Presence and SOURCE.

    These four lines in `doctor` read `os.environ` directly and reported
    `unset` for a secret that was present in `config.toml` and that the desk
    was about to use (straightedge#139). A false negative about a credential
    is worse than the documentation gap the issue was filed about: the
    operator reads "telegram token: unset", concludes the desk is not
    configured, and goes looking for a problem that is not there.

    So the check now reads the EFFECTIVE value, the one the desk will use, and
    names where it came from. Presence only; a value is never printed, exactly
    as `mt4_transport_line` already does for the mailbox token.
    """
    if not value:
        return "unset"
    return f"SET ({'config.toml' if toml_key in cfg.settings_from_file else 'env'})"


def mt4_transport_line(cfg: BotConfig) -> str:
    """Which transport THIS config will use, and whether its secret is present.

    Two configs can differ only in an environment variable here, and an operator
    reading `doctor` has to be able to tell a co-located desk from a remote one
    without inspecting the process environment by hand. `mailbox_url` wins over
    `files_dir` in `broker_for`, so this reports the one that will actually be
    used rather than both. The token is presence-checked and never printed.
    """
    budgets = (
        f"budgets: {cfg.mt4.timeout_ms}ms read / {cfg.mt4.send_timeout_ms}ms send"
    )
    if cfg.mt4.mailbox_url:
        token = "SET" if cfg.mt4.mailbox_token else "unset"
        return (
            f"mt4 transport: network shim {cfg.mt4.mailbox_url} "
            f"({TOKEN_ENV}: {token}), {budgets}"
        )
    return (
        f"mt4 transport: file mailbox, files_dir: "
        f"{cfg.mt4.files_dir or 'unset'}, {budgets}"
    )


def cmd_mt4_shim(args: argparse.Namespace) -> int:
    """Serve this host's MT4 mailbox to a remote desk. See docs/TRANSPORT.md.

    Runs ON the MetaTrader 4 host, beside the terminal, and is the only thing of
    ours that has to. It holds no risk logic, no prompts, no model keys and no
    journal, so a bug fix to any of those does not touch the customer's box.
    """
    cfg = load_config(args.config) if args.config else load_config()
    try:
        server = make_shim(
            files_dir=cfg.mt4.files_dir,
            token=cfg.mt4.mailbox_token,
            host=args.host,
            port=args.port,
            timeout_sec=max(1.0, cfg.mt4.timeout_ms / 1000.0),
            # BOTH budgets, or the shim becomes the end that gives up first.
            # `FileBridge.exchange` sizes its mailbox wait from the request's own
            # `ttl_ms` CLAMPED to this ceiling, so a shim built with only the read
            # budget clamps a 7060ms send down to 5000ms and abandons a request the
            # Expert is still executing about 1.9s before the desk would have. That
            # is the defect the split budget removes, reintroduced one hop away,
            # and it is reachable only on the network transport, which is the live
            # topology. Guarded by
            # `tests/test_mt4_net_transport.py::test_the_shim_cli_gives_the_mailbox_the_desks_send_ceiling`.
            send_timeout_sec=max(1.0, cfg.mt4.send_timeout_ms / 1000.0),
            allow_plaintext_exposure=args.i_understand_plaintext,
        )
    except (RuntimeError, OSError) as exc:
        print(redact_text(str(exc)), file=sys.stderr)
        return 2
    return serve(server)


def watchdog_line(cfg: BotConfig) -> str:
    """The derived staleness threshold, printed where an operator will see it.

    `doctor` is the one command the runbook makes mandatory, so the number the
    watchdog will judge this desk by is stated there rather than left to be
    discovered from a source file.
    """
    line = (
        f"watchdog: {heartbeat_path_for(cfg.journal_path)}, stale after "
        f"{stale_after_seconds(cfg)}s (tick budget {tick_budget_seconds(cfg)}s)"
    )
    if not cfg.telegram.enabled:
        # The long poll is the biggest term in the budget, and with no token
        # there is no long poll, so this number is not the one a running desk
        # will get. `run` refuses to start without Telegram anyway; printing the
        # bare figure would be a measurement of a config that cannot run.
        line += " -- telegram unset, so the poll term is missing from this figure"
    return line


def cmd_watch(args: argparse.Namespace) -> int:
    """Is the desk ticking, and will it trade. See docs/RUNBOOK.md `Watchdog`.

    A SEPARATE process on purpose. A desk cannot report its own death, and an
    in-process staleness check is an instrument that fails with its subject;
    this one shares nothing with the desk but the heartbeat file, and it never
    takes the run lock (that could make a restarting desk exit `already
    running`) and never calls `getUpdates` (that would steal the desk's
    commands).
    """
    cfg = _cfg(args)
    path = heartbeat_path_for(cfg.journal_path)
    # SEND-ONLY, and the missing `offset_path=` is the load-bearing part: this
    # client is never given a cursor because it must never poll for updates.
    tg = TelegramClient.from_config(cfg.telegram)
    if tg is None and args.loop:
        print(
            "telegram is the alarm channel: set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID, or run `watch` once and read the exit code",
            file=sys.stderr,
        )
        return 2
    if tg is None:
        print("telegram disabled: this check prints and exits, it cannot alert")
    print(watchdog_line(cfg))
    return watch(
        path,
        cfg,
        send=(tg.send if tg is not None else None),
        loop=bool(args.loop),
        ok_every=float(args.ok_every),
    )


def cmd_supervision(args: argparse.Namespace) -> int:
    """Are the scheduled tasks that keep the desk alive actually supervision.

    READ-ONLY. It parses task definitions that something else dumped; it never
    registers, starts, stops or edits a task, and it never touches the desk,
    `journal.lock` or Telegram. Safe to run against a live box mid-session,
    which is the whole point: an audit an operator is afraid to run during
    trading hours is an audit that gets run after the outage.

    The interval ceiling is derived from the config THIS invocation loaded, so
    running it on the box measures the box. See `straightedge.supervision`.
    """
    cfg = _cfg(args)
    ceiling, measurable = supervision.interval_ceiling(cfg)
    source = Path(args.tasks)
    if not source.exists():
        print(
            f"supervision: no such path {source}. Dump the live definitions "
            "first with deploy\\windows\\Export-Tasks.ps1, or point --tasks at "
            "deploy/windows to audit the DECLARED ones. An audit with nothing "
            "to read is not a pass",
            file=sys.stderr,
        )
        return 2
    views = supervision.load_views(source)
    findings = supervision.audit(
        views, max_interval_s=ceiling, measurable=measurable
    )
    print(
        supervision.report(
            findings, max_interval_s=ceiling, measurable=measurable
        )
    )
    return supervision.exit_code(findings)


def telegram_ping(cfg: BotConfig, *, transport=None) -> str:
    tg = TelegramClient.from_config(
        cfg.telegram,
        transport=transport,
        offset_path=offset_path_for(cfg.journal_path),
    )
    if tg is None or not tg.enabled:
        return "skip"
    try:
        ok = tg.send("straightedge doctor ping")
    except (ValueError, RuntimeError, OSError) as exc:
        return f"fail ({exc})"
    return "ok" if ok else "fail"


def venue_clock_check(cfg: BotConfig, broker: object) -> int:
    """Say which clock the desk is timing itself off. Returns an exit code.

    Why it exists. The straightedge#172 defect was INVISIBLE: the operator
    read `start_utc = "07:00"` in the config, the desk timed its auto leg off
    a UTC+3 bar stamp, and nothing anywhere printed the three hours between
    those two facts. The offset is now measured, so it can be stated, and a
    measurement that nothing ever displays is one nobody can check against
    the server's own clock.

    Why NOT MEASURED exits non-zero, like `history_check` above: an auto leg
    that cannot measure WHEN it is refuses every signal, by design. That is a
    run-affecting condition, so doctor must not clear a run while it holds.
    """
    clock = venue_clock_of(broker, cfg.symbols[0] if cfg.symbols else "EURUSD")
    if not clock.measured:
        print(
            "venue clock: NOT MEASURED ("
            + ", ".join(sorted(clock.unmeasured))
            + (f": {clock.detail}" if clock.detail else "")
            + "). The auto leg REFUSES every signal while this holds, because "
            "reading a broker stamp as UTC is straightedge#172."
        )
        return 1
    offset = clock.offset_sec or 0
    sign = "+" if offset >= 0 else "-"
    hh, mm = divmod(abs(offset) // 60, 60)
    taken = (
        datetime.fromtimestamp(clock.measured_at, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        if clock.measured_at
        else "construction"
    )
    print(
        f"venue clock: server UTC{sign}{hh:02d}:{mm:02d} measured "
        f"({clock.source}, at {taken}). Bar times are converted to UTC with "
        "this before any gate sees them."
    )
    return 0


def history_check(cfg: BotConfig, broker: object) -> int:
    """Per-symbol bars and ATR against a live terminal. Returns an exit code.

    Why this is here at all. A price series on MT4 exists per symbol AND
    timeframe, and the terminal builds one only when something asks for it, so a
    config on H1 in a terminal with H4 charts has H1 history for nothing. That
    was measured on the live rig as EURUSD bars=0 / ATR=nan, and nothing in the
    whole toolchain said so: `doctor` reported nothing about history, and the
    desk answered by not trading. This is the instrument that makes it a
    statement.

    Why it exits NON-ZERO. `doctor`'s own help calls it the pre-run gate, and
    #33 B1 makes `exit 0` part of the pass condition before any run. A
    configured symbol that cannot produce a signal is a run-affecting condition,
    so exit 0 would be doctor clearing a run while a named instrument is dead.
    The "annoying red for a symbol I never trade" case does not exist here:
    `[symbols] names` is the SCAN list that drives the loop and /quote
    (`config.py:247-253`), so a name in it IS a symbol the desk will try to
    trade. Advice-only names have their own key, `advice.symbols`. A red here is
    therefore either a dead instrument or a misconfigured list, and both should
    stop a run.

    It never fabricates. A symbol with no series is reported with no series; no
    default ATR, no synthetic bar, nothing borrowed from another symbol
    (`broker/mt4_live.py:294-308` is what that cost last time).
    """
    needed = TrendStrategy(cfg.strategy).needed_bars()
    report = preflight(
        broker,
        cfg.symbols,
        timeframe=cfg.strategy.timeframe,
        needed=needed,
        atr_period=cfg.strategy.atr_period,
    )
    print(
        f"history: {cfg.strategy.timeframe}, need {needed} bars, "
        f"{len(report.symbols) - len(report.unusable)} of {len(report.symbols)} usable"
    )
    for text in report.lines():
        print(f"  {text}")
    if report.ok:
        return 0
    print(report.text())
    return 1


def paper_round_trip() -> str:
    """In-process /buy /confirm /close. No live terminal."""
    with TemporaryDirectory() as tmp:
        cfg = BotConfig()
        cfg.session.enabled = False
        cfg.risk.max_spread_atr_frac = 10.0
        cfg.risk.halt_file = str(Path(tmp) / "HALT")
        cfg.journal_path = str(Path(tmp) / "j.jsonl")
        cfg.symbols = ["EURUSD"]
        broker = broker_for(cfg)
        # mode is hardcoded "paper" a few lines up; make that guarantee
        # explicit rather than relying on Broker's abstract interface to
        # happen to have seed_bars (it does not -- PaperBroker-only).
        assert isinstance(broker, PaperBroker)
        broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
        engine = Engine(
            cfg,
            broker,
            halt_dir=tmp,
            now_fn=lambda: datetime(2024, 1, 3, 12, tzinfo=timezone.utc),
        )
        engine.start()
        buy = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
        if "confirm buy" not in buy:
            engine.stop()
            return f"fail stage: {buy}"
        sent = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
        if not sent.startswith("sent buy"):
            engine.stop()
            return f"fail confirm: {sent}"
        if not engine.broker.positions():
            engine.stop()
            return "fail confirm: no position"
        closed = engine.handle_command(TgCommand("1", 1, "/close all", 3))
        if not closed.startswith("closed"):
            engine.stop()
            return f"fail close: {closed}"
        if engine.broker.positions():
            engine.stop()
            return "fail close: position remains"
        engine.stop()
        return "ok"


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"straightedge {__version__}")
    print(f"python {sys.version.split()[0]}  {sys.executable}")
    mt5_ok = False
    try:
        from straightedge.broker.mt5_live import load_mt5_module

        mod = load_mt5_module()
        mt5_ok = True
        print(f"mt5 binding: {getattr(mod, '__name__', 'unknown')}")
    except Exception as exc:
        print(f"mt5 binding: unavailable ({exc})")
    cfg = load_config(args.config) if args.config else load_config()
    if args.config:
        print(f"config: mode={cfg.mode} symbols={cfg.symbols} risk_pct={cfg.risk.risk_pct}")
    print(_posture_line(cfg))
    print(_settings_source_line(cfg))
    print("terminal: official MetaTrader5 package is Windows-only.")
    print("macOS: install MetaTrader 5.app from metatrader5.com, then pip install mt5-mac.")
    print("MT4: attach mt4/Experts/Mt4RiskBot.mq4. files_dir is Common Files.")
    print("Windows default: %APPDATA%\\MetaQuotes\\Terminal\\Common\\Files")
    if cfg.mode == "mt4":
        print(mt4_transport_line(cfg))
    print(watchdog_line(cfg))
    print("Homebrew has no MetaTrader cask; Python is enough for paper/backtest.")
    # The EFFECTIVE value with its source, never os.environ alone: see
    # `_presence`. A secret in config.toml used to read here as `unset`.
    print("telegram token:", _presence(cfg, "telegram.token", cfg.telegram.token))
    print("telegram chat:", _presence(cfg, "telegram.chat_id", cfg.telegram.chat_id))
    print("xai key:", _presence(cfg, "advice.grok_key", cfg.advice.grok_key))
    print("anthropic key:", _presence(cfg, "advice.claude_key", cfg.advice.claude_key))
    print("ai provider:", cfg.advice.provider)
    ping = telegram_ping(cfg)
    print(f"telegram ping: {ping}")
    paper = paper_round_trip()
    print(f"paper round-trip /buy /confirm /close: {paper}")
    rc = 0
    if ping.startswith("fail") or paper != "ok":
        rc = 1
    if not args.connect:
        # An absent check reads exactly like a passed one, so say it was not
        # run. Per-symbol history can only be measured against a live terminal.
        print(
            f"history: NOT MEASURED for {len(cfg.symbols)} configured symbol(s); "
            "needs --connect and a live terminal"
        )
    if args.connect:
        cfg = _cfg(args) if args.config else load_config()
        if cfg.mode == "mt4":
            broker = None
            try:
                broker = broker_for(cfg)
                broker.connect()
                acct = broker.account()
                print(
                    f"connected venue=mt4 login={mask_account_id(acct.login)} "
                    f"server={acct.server} "
                    f"equity={acct.equity:.2f} {acct.currency} trade_mode={acct.trade_mode}"
                )
                # The two halves of the send contract, read off the LIVE Expert
                # rather than off this repo's copy of it. The Expert's retry
                # ladders arrive as `input` parameters, so the attached Expert can
                # differ from the shipped one with nothing saying so. A budget
                # that does not clear the ladder is the condition that makes a
                # duplicate order reachable, so `doctor` goes RED on it and does
                # not merely mention it.
                fence = getattr(broker, "send_fence_report", None)
                if callable(fence):
                    line = fence()
                    print(line)
                    if "TOO SHORT" in line:
                        rc = 1
                if venue_clock_check(cfg, broker):
                    rc = 1
                if history_check(cfg, broker):
                    rc = 1
            except (RuntimeError, OSError, ValueError) as exc:
                print(f"connect: fail ({redact_text(str(exc))})")
                rc = 1
            finally:
                if broker is not None:
                    try:
                        broker.disconnect()
                    except (RuntimeError, OSError, ValueError, AttributeError):
                        pass
            return rc
        if not mt5_ok:
            print("connect: fail (no mt5 binding)")
            return 1
        broker = broker_for(replace(cfg, mode="mt5"))
        try:
            ensure = getattr(broker, "ensure_connected", None)
            if callable(ensure):
                ensure()
            else:
                broker.connect()
            acct = broker.account()
            print(
                f"connected login={mask_account_id(acct.login)} server={acct.server} "
                f"equity={acct.equity:.2f} {acct.currency} trade_mode={acct.trade_mode}"
            )
            if venue_clock_check(cfg, broker):
                rc = 1
            if history_check(cfg, broker):
                rc = 1
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"connect: fail ({redact_text(str(exc))})")
            rc = 1
        finally:
            try:
                broker.disconnect()
            except (RuntimeError, OSError, ValueError, AttributeError):
                pass
    return rc


def _load_csv(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            bars.append(
                Bar(
                    time=int(float(row["time"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    tick_volume=int(float(row.get("tick_volume") or 0)),
                    spread=int(float(row.get("spread") or 0)),
                )
            )
    return bars


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    if args.no_session_filter:
        cfg.session.enabled = False
    series: dict[str, list[Bar]] = {}
    if args.csv:
        p = Path(args.csv)
        name = args.symbol or cfg.symbols[0]
        series[name] = _load_csv(p)
        cfg.symbols = [name]
    else:
        kind = args.market
        for i, name in enumerate(cfg.symbols):
            if kind == "range":
                series[name] = generate_ranging(args.bars, seed=10 + i)
            else:
                drift = 0.00025 if kind == "trend" else 0.0
                series[name] = generate_bars(args.bars, drift=drift, seed=1 + i)
    journal = args.journal or cfg.journal_path
    result = run_backtest(cfg, series, journal_path=journal)
    start = cfg.initial_balance
    ret = (result["equity"] - start) / start
    print(json.dumps({"start": start, "return": round(ret, 6), **result, "journal": journal}, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    if args.mode:
        cfg.mode = args.mode
    print(_posture_line(cfg))
    print(_settings_source_line(cfg))
    try:
        lock = InstanceLock(cfg.journal_path)
        lock.acquire()
    except InstanceLockError:
        print("already running", file=sys.stderr)
        return 2
    try:
        return _cmd_run_locked(args, cfg)
    finally:
        lock.release()


def _cmd_run_locked(args: argparse.Namespace, cfg: BotConfig) -> int:
    broker = broker_for(cfg)
    if cfg.mode == "paper":
        if args.synthetic:
            from straightedge.engine import run_backtest as _bt
            from straightedge.synthetic import generate_bars as _gb

            series = {
                name: _gb(800, drift=0.0002, seed=3 + i) for i, name in enumerate(cfg.symbols)
            }
            cfg.session.enabled = False
            result = _bt(cfg, series, journal_path=cfg.journal_path)
            print(json.dumps({"mode": "paper-synthetic", **result}, indent=2))
            return 0
        elif args.feed_mt5:
            live = broker_for(replace(cfg, mode="mt5"))
            live.connect()
            # This whole branch is under `if cfg.mode == "paper":` above, so
            # broker is the PaperBroker constructed a few lines earlier.
            assert isinstance(broker, PaperBroker)
            for name in cfg.symbols:
                live.select_symbol(name)
                rates = live.rates(name, cfg.strategy.timeframe, 400)
                broker.seed_bars(name, rates)
            live.disconnect()
            print("paper broker seeded from MT5 history; orders stay local")

    halt_dir = str(Path(cfg.risk.halt_file).parent) or "."
    tg = TelegramClient.from_config(
        cfg.telegram, offset_path=offset_path_for(cfg.journal_path)
    )
    if tg is None:
        print("telegram is the front door: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return 2
    engine = Engine(cfg, broker, halt_dir=halt_dir, telegram=tg)
    engine.start()
    # `start()` already asked the venue for every configured symbol's series,
    # which on MT4 is what makes the terminal fetch it. Anything still unusable
    # here will never produce a signal, so the run does not begin pretending it
    # will: the symbols are named on stderr and the process exits non-zero.
    # Ordered preference is it-just-works first and a named failure second; a
    # silent non-trade is not on the list at all.
    report = engine.history
    if report is None:
        # `start()` always measures, so this is reachable only from a test
        # double. Unmeasured is reported as unmeasured, never as healthy.
        print("history: NOT MEASURED (engine reported no preflight)", file=sys.stderr)
    elif not report.ok:
        print(report.text(), file=sys.stderr)
        engine.stop()
        return 2
    try:
        run_loop(engine, loop=bool(args.loop), keep_on_halt=True)
    except KeyboardInterrupt:
        print("interrupt")
    finally:
        engine.stop()
    return 0


def run_loop(engine: Engine, *, loop: bool, keep_on_halt: bool = True) -> None:
    """One tick, or until Ctrl-C. A bad tick is journaled; the process stays up."""
    while True:
        try:
            engine.step_all()
        except Exception as exc:
            print(f"loop error: {redact_text(str(exc))}", file=sys.stderr)
            try:
                engine.journal.write("loop_error", error=str(exc)[:200])
            except Exception:
                pass
        if engine.halted and not keep_on_halt:
            print("halted")
            break
        if not loop:
            break


def cmd_telegram(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    tg = TelegramClient.from_config(cfg.telegram)
    if tg is None:
        print("telegram disabled: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return 2
    ok = tg.send(args.message or "straightedge ping")
    print("sent" if ok else "send failed")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="straightedge",
        description="Telegram desk for MT4/MT5: full trades and Grok/Claude advice. Risk gates every order.",
    )
    p.add_argument("--config", help="path to TOML config")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser(
        "doctor",
        help="telegram ping, paper /buy /confirm /close, optional venue login",
    )
    d.add_argument("--connect", action="store_true")
    d.set_defaults(func=cmd_doctor)

    b = sub.add_parser("backtest", help="run on synthetic or CSV bars")
    b.add_argument("--bars", type=int, default=1500)
    b.add_argument("--market", choices=("trend", "range", "flat"), default="trend")
    b.add_argument("--csv", help="OHLC CSV with columns time,open,high,low,close")
    b.add_argument("--symbol", help="symbol name for CSV")
    b.add_argument("--journal", help="journal jsonl path")
    b.add_argument("--no-session-filter", action="store_true")
    b.set_defaults(func=cmd_backtest)

    r = sub.add_parser("run", help="paper, mt5, or mt4 loop")
    r.add_argument("--mode", choices=("paper", "mt5", "mt4"))
    r.add_argument("--loop", action="store_true", help="poll until halt or Ctrl-C")
    r.add_argument("--synthetic", action="store_true", help="seed paper broker with generated bars")
    r.add_argument("--feed-mt5", action="store_true", help="seed paper broker from a live terminal")
    r.add_argument(
        "--i-accept-risk",
        action="store_true",
        help="required to send orders on a real (trade_mode=2) account",
    )
    r.set_defaults(func=cmd_run)

    s = sub.add_parser(
        "mt4-shim",
        help="serve THIS host's MT4 mailbox to a remote desk (run it on the MT4 box)",
    )
    s.add_argument(
        "--host",
        default="127.0.0.1",
        help="listen address. Non-loopback needs --i-understand-plaintext",
    )
    s.add_argument("--port", type=int, default=DEFAULT_SHIM_PORT)
    s.add_argument(
        "--i-understand-plaintext",
        action="store_true",
        help=(
            "bind a routable address with no TLS. The supported exposure is a "
            "Cloudflare Tunnel, which needs no inbound port at all"
        ),
    )
    s.set_defaults(func=cmd_mt4_shim)

    w = sub.add_parser(
        "watch",
        help="is the desk ticking, and is it armed (reads journal.heartbeat)",
    )
    w.add_argument(
        "--loop",
        action="store_true",
        help="keep watching and alert the locked chat on every state change",
    )
    w.add_argument(
        "--ok-every",
        type=float,
        default=0.0,
        help=(
            "seconds between confirmations that the desk is healthy. 0 is off. "
            "Set it and this watcher going silent becomes a signal too"
        ),
    )
    w.set_defaults(func=cmd_watch)

    v = sub.add_parser(
        "supervision",
        help="do the Windows scheduled tasks actually restart and watch the desk",
    )
    v.add_argument(
        "--tasks",
        default="deploy/windows",
        help=(
            "directory of <task-name>.xml dumps, or one dump file. Defaults to "
            "the DECLARED definitions in deploy/windows; point it at the output "
            "of deploy/windows/Export-Tasks.ps1 to audit the live box"
        ),
    )
    v.set_defaults(func=cmd_supervision)

    t = sub.add_parser("telegram", help="send a test message to the configured chat")
    t.add_argument("--message", default="straightedge ping")
    t.set_defaults(func=cmd_telegram)
    return p


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
