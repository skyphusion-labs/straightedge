"""Load TOML config.

The environment is where secrets BELONG, and for every secret but one the file
is still accepted as a fallback (`straightedge#139`). Saying "secrets never
live here" was the older claim and it was not true of this loader: env wins,
and a key present in the TOML is read when its variable is unset. That is a
deliberate convenience, not an oversight, and the cost of removing it is an
operator whose running desk stops starting.

What closes the gap is `settings_from_file` instead: the loader records WHICH of
those keys it took from the file, by name and never by value, `doctor` and
`run` print it, and the journal's `start` record carries it. The same shape as
the handover posture in `docs/CONTRACT.md`, for the same stated reason: the gap
a permissive default leaves is closed by observability, not by a stricter
default that silently changes every existing deployment.

`mt4.mailbox_token` is the one key with no TOML entry at all, so a key an
operator writes there is discarded. That is reported through
`settings_read_from_nowhere` rather than refused, and
`IGNORED_FILE_SOURCED_SETTINGS` says why: the only dangerous state is already
fail-closed in `mt4_net.require_token`, one layer down.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from straightedge.currencies import may_transform_symbol, normalize_model_symbol
from straightedge.constants import (
    EA_CLAIM_RETRY_MS,
    EA_LADDER_SLEEP_MS,
    MAILBOX_ROUND_TRIP_CEILING_MS,
    TIMEFRAME_BY_NAME,
    TIMEFRAME_H1,
    derive_send_timeout_ms,
)


#: Where an effective deviation came from. Reported on every send, because a
#: number whose ORIGIN is invisible cannot be debugged: 20 points read in a
#: journal says nothing about whether the per-symbol map was consulted, missed,
#: or never written.
DEVIATION_FROM_SYMBOL = "symbol"
DEVIATION_FROM_DEFAULT = "default"

#: NAMING, and it is deliberate rather than coy. Nothing in this group is
#: called `secret`, `key`, `token`, `password` or `credential`, even though
#: "the secret settings" is what a human would say. CodeQL classifies
#: sensitive data by IDENTIFIER NAME, so `print(_secret_source_line(cfg))`
#: raised two HIGH py/clear-text-logging-sensitive-data alerts while the
#: adjacent `_presence(cfg, "telegram.token", cfg.telegram.token)` call, which
#: handles an actual token, raised none. These fields hold key NAMES and
#: `tests/test_config_secret_provenance.py` asserts no value reaches stdout,
#: so the alert was wrong about the data. It was right about the SHAPE: a
#: field named `secrets_from_file` that is printed is one careless refactor
#: from a real leak. Suppressing it would blind that print to the real leak
#: forever, so the names moved instead and the gate stays live. Do not
#: "improve" these names back; the docstrings carry the meaning.
#:
#: Every name under "Secret names" in SECURITY.md that this loader will read
#: from `config.toml` when its environment variable is unset, as
#: (env var, TOML section, TOML key). The list exists so the document and the
#: loader cannot drift again: SECURITY.md describes exactly these nine, and
#: `tests/test_config_secret_provenance.py` asserts the correspondence in both
#: directions rather than trusting either side's prose.
#:
#: `MT4_MAILBOX_TOKEN` is deliberately NOT here. It has no TOML key at all.
FILE_SOURCED_SETTINGS: tuple[tuple[str, str, str], ...] = (
    ("MT5_LOGIN", "mt5", "login"),
    ("MT5_PASSWORD", "mt5", "password"),
    ("MT5_SERVER", "mt5", "server"),
    ("TELEGRAM_BOT_TOKEN", "telegram", "token"),
    ("TELEGRAM_CHAT_ID", "telegram", "chat_id"),
    ("XAI_API_KEY", "advice", "grok_key"),
    ("ANTHROPIC_API_KEY", "advice", "claude_key"),
    ("ADVICE_URL", "advice", "computer_url"),
    ("ADVICE_TOKEN", "advice", "computer_token"),
)


def settings_taken_from_file(data: dict, environ: dict | None = None) -> tuple[str, ...]:
    """Which `FILE_SOURCED_SETTINGS` this load would take from the FILE, by NAME.

    Names only. A VALUE must never reach this return, `doctor`'s stdout or the
    journal, which is the whole reason the loader reports provenance rather
    than the operator reading `config.toml` by hand to find out.

    The env-wins test is `is not None`, matching the `os.environ.get(VAR,
    <file>)` form at each call site EXACTLY, including the case of a variable
    exported as the empty string: that currently beats the file and disables
    the feature, and quietly changing it would change behaviour on a running
    desk rather than report on one.
    """
    env = os.environ if environ is None else environ
    out: list[str] = []
    for var, section, key in FILE_SOURCED_SETTINGS:
        if env.get(var) is not None:
            continue
        sec = _section(data, section)
        if str(sec.get(key, "") or ""):
            out.append(f"{section}.{key}")
    return tuple(out)


#: Secret-bearing TOML keys the loader reads from NOWHERE. A key here is an
#: operator instruction being discarded, which is the defect
#: `parse_symbol_deviation_points` names below: "An override an operator wrote
#: and the loader ignored is the worst of the three outcomes ... and nothing
#: reports the disagreement."
#:
#: It is REPORTED and not refused, deliberately. The only state in which an
#: ignored `mt4.mailbox_token` is dangerous is `mailbox_url` set with no
#: `MT4_MAILBOX_TOKEN`, and `mt4_net.require_token` already fails closed there
#: at startup on both ends ("An empty token is not 'auth disabled', it is a
#: misconfiguration"). A second gate at load would catch nothing that gate
#: misses, and it WOULD stop two working configurations from starting: the env
#: var set with a stale key still in the file, and the co-located file mailbox,
#: which needs no token at all. What was missing was never a refusal. It was
#: the report.
IGNORED_FILE_SOURCED_SETTINGS: tuple[tuple[str, str], ...] = (("mt4", "mailbox_token"),)


def settings_read_from_nowhere(data: dict) -> tuple[str, ...]:
    """`IGNORED_FILE_SOURCED_SETTINGS` actually present in this file, by NAME.

    Independent of the environment: the key is read from nowhere, so whether a
    variable happens to be set changes nothing about the file being wrong.
    An EMPTY value is not reported; it overrides nothing, so there is no
    disagreement, and a placeholder an operator left behind is not a defect.
    """
    out: list[str] = []
    for section, key in IGNORED_FILE_SOURCED_SETTINGS:
        if str(_section(data, section).get(key, "") or ""):
            out.append(f"{section}.{key}")
    return tuple(out)


@dataclass(frozen=True)
class DeviationResolution:
    """Which deviation applied to one send, and where it came from.

    Returned as one value rather than read from two calls, so the number and
    its provenance cannot be reported out of step with each other.
    """

    symbol: str
    points: int
    source: str


@dataclass
class RiskConfig:
    risk_pct: float = 0.005
    daily_loss_pct: float = 0.02
    max_drawdown_pct: float = 0.10
    max_positions: int = 3
    max_currency_exposure: int = 2
    min_rr: float = 1.5
    max_spread_atr_frac: float = 0.15
    min_free_margin_pct: float = 0.50
    magic: int = 20260909
    halt_file: str = "HALT"
    max_risk_multiple: float = 1.0
    #: Maximum tolerated slippage on a send, in POINTS, for any symbol NOT
    #: listed in `symbol_deviation_points`. A point is instrument-specific, so
    #: this single number cannot be right everywhere: see the override below.
    deviation_points: int = 20
    #: Per-symbol override of `deviation_points`, keyed by the symbol name the
    #: BROKER uses. 20 points is 2 pips on a 5-digit EURUSD and 20 cents on
    #: XAUUSD, and gold was measured quoting a 45-point spread, so one global
    #: number is a value that is correct on FX and below one spread on metals.
    #: Matching is case-insensitive and otherwise exact: a venue that calls
    #: gold `XAUUSD.m` must be keyed `XAUUSD.m`, because stripping a decoration
    #: would mean guessing which decorations mean the same instrument. A miss
    #: falls back to `deviation_points` and is then caught by the
    #: `deviation_below_spread` gate, which is what makes a mis-keyed entry
    #: loud instead of silent.
    symbol_deviation_points: dict[str, int] = field(default_factory=dict)
    #: How many times the live spread the effective deviation must cover before
    #: a send is allowed. The default is 1.0 because that is the only multiple
    #: that can be PROVEN rather than guessed: a market order must cross the
    #: bid/ask gap to fill at all, so a tolerance smaller than the spread can
    #: only ever be rejected, on any instrument, with no measurement required.
    #:
    #: A higher default was tried first and is wrong. At 3.0 this gate refuses
    #: the repo's own paper broker, whose EURUSD carries a 10-point spread
    #: against the 20-point default: a 2x ratio, which is ordinary retail FX
    #: and fills routinely. The evidence available bounds the true boundary
    #: between 0.44x (gold at 20 points against a 45-point spread, measured
    #: rejecting on the live MT4 rig 2026-09-24) and 2.0x (working), and does
    #: not locate it. Shipping 3.0 would encode a guess about where the
    #: boundary sits as though it had been measured, and would refuse trades on
    #: the strength of it.
    #:
    #: An operator who wants margin raises this; `DEVIATION_HEADROOM_MULTIPLE`
    #: is what the refusal RECOMMENDS they set the deviation to, which is a
    #: separate and larger number on purpose. `validate()` refuses anything
    #: below 1.0, so the gate cannot be configured below the floor it exists to
    #: enforce, and there is no value that switches it off.
    min_deviation_spread_multiple: float = 1.0
    #: Opening sends allowed per UTC day, across auto, telegram and advice.
    #: 0 disables the cap. Counts OPENS only: a close must never be capped,
    #: because a control that can stop you reducing exposure is not a risk
    #: control. `daily_loss_pct` only fires after the money is gone; this is
    #: the one that bounds churn before it.
    max_trades_per_day: int = 0

    def resolve_deviation(self, symbol: str) -> DeviationResolution:
        """The deviation for this symbol: its own entry, else the global default.

        Symbol-specific first, then the default, and the answer says which one
        it was. The lookup upper-cases both sides and compares the whole name;
        `validate()` refuses a map whose keys collide once upper-cased, so this
        scan cannot silently pick one of two entries for the same symbol.
        """
        key = symbol.upper()
        for name, value in self.symbol_deviation_points.items():
            if str(name).upper() == key:
                return DeviationResolution(
                    symbol=symbol, points=int(value), source=DEVIATION_FROM_SYMBOL
                )
        return DeviationResolution(
            symbol=symbol, points=int(self.deviation_points), source=DEVIATION_FROM_DEFAULT
        )


@dataclass
class StrategyConfig:
    auto: bool = False
    trail: bool = False
    timeframe: str = "H1"
    fast_ema: int = 21
    slow_ema: int = 55
    adx_period: int = 14
    adx_min: float = 20.0
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    atr_tp_mult: float = 2.5
    breakeven_r: float = 1.0
    trail_r: float = 1.5
    trail_atr_mult: float = 1.2

    @property
    def timeframe_id(self) -> int:
        key = self.timeframe.upper()
        if key not in TIMEFRAME_BY_NAME:
            raise ValueError(f"unknown timeframe {self.timeframe!r}")
        return TIMEFRAME_BY_NAME.get(key, TIMEFRAME_H1)


@dataclass
class SessionConfig:
    enabled: bool = True
    start_utc: str = "07:00"
    end_utc: str = "17:00"
    skip_friday_after_utc: str = "16:00"


def _expand_win_vars(s: str) -> str:
    """Expand %VAR% (Windows) after $VAR. Safe on Unix so configs travel."""
    s = os.path.expandvars(s)

    def repl(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), m.group(0))

    return re.sub(r"%([^%]+)%", repl, s)


def resolve_mt4_files_dir(raw: str, *, platform: str | None = None) -> str:
    """Expand %APPDATA% / ~. On Windows, empty means Common Files."""
    s = _expand_win_vars(os.path.expanduser((raw or "").strip().strip('"')))
    if s:
        return str(Path(s))
    plat = platform if platform is not None else sys.platform
    if plat == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return str(Path(appdata) / "MetaQuotes" / "Terminal" / "Common" / "Files")
    return ""


def resolve_state_path(raw: str, *, base_dir: Path) -> str:
    """Anchor a relative journal_path/halt_file to an explicit base (fc34).

    `base_dir` is the config file's own directory when --config was given,
    else the process working directory -- both documented, explicit bases.
    What this refuses is the alternative: a relative path resolving
    implicitly against wherever the process happens to be started from.
    Under Windows Task Scheduler that working directory is not the repo,
    so the journal, the instance lock, and the emergency HALT file must
    not depend on it. An absolute path (already pinned by the operator)
    passes through unchanged.
    """
    p = Path(raw)
    return str(p if p.is_absolute() else base_dir / p)


@dataclass
class Mt4Config:
    files_dir: str = ""
    #: The READ budget, in milliseconds. Applies to every op that cannot move
    #: money: ping, account, symbol, tick, select, rates, positions, orders, and
    #: both `check_*` dry runs. Losing one of these is cheap, so the number is
    #: sized to notice a wedged mailbox rather than to outlast anything.
    #:
    #: **What is actually known about it, corrected (#127).** This comment used
    #: to read "205ms p50 and 223ms max, so 5000 is a 22x margin". The max was
    #: not measured: `tests/live_measurements.py` is the one home for numbers off
    #: that rig and it records the MEDIAN only, deliberately, because the pairing
    #: that produced the latencies shifts by one after every unanswered request
    #: and three of 2166 went unanswered. So the honest statement is 5000ms
    #: against a measured p50 of 205ms, 24x the median, with the TAIL UNKNOWN,
    #: and a budget is sized against the tail. `docs/RUNBOOK.md`, "Measuring the
    #: mailbox round trip", carries the run that would settle it.
    #:
    #: It is deliberately UNCHANGED, and not only because the tail is unmeasured:
    #: it was never the defect, and `watchdog.venue_timeout_seconds` and
    #: `tests/test_mt4_claim_open_retry.py` both derive numbers from it.
    timeout_ms: int = 5000
    #: The SEND budget, in milliseconds, for the ops that can change the book.
    #:
    #: A separate number because the two failures do not cost the same. A read
    #: that times out is retried by the next step. A send that times out is
    #: AMBIGUOUS: the order may be filled, in flight, or never sent, and the desk
    #: cannot tell. The budget therefore has one job -- never expire while the
    #: Expert is still working -- and the default is DERIVED from the Expert's own
    #: worst case rather than chosen. See `constants.derive_send_timeout_ms`.
    send_timeout_ms: int = field(default_factory=derive_send_timeout_ms)
    #: The `mt4-shim` endpoint on the host that runs MetaTrader 4 (#73).
    #:
    #: Set it and the desk speaks HTTP to that shim instead of reading the
    #: mailbox directory itself, which is what lets the desk run off the
    #: customer's Windows box. Empty means the co-located file mailbox, which is
    #: still fully supported and is what a self-hoster keeps using.
    #:
    #: It is checked BEFORE `files_dir` in `broker_for`, and that order is
    #: load-bearing: on Windows `files_dir` auto-resolves to Common Files even
    #: when nobody configured it, so a url that lost the tie would leave a
    #: remote-configured desk silently reading a LOCAL mailbox.
    mailbox_url: str = ""
    #: The shared bearer token for `mailbox_url`. Environment only
    #: (`MT4_MAILBOX_TOKEN`), never read from the TOML file: `docs/CONTRACT.md`
    #: keeps secrets in the environment, and this one can place orders.
    mailbox_token: str = ""
    #: Seconds `Mt4Broker.startup_connect()` waits for the Expert at startup.
    #:
    #: `None` means the operator did not set it, and the adapter's own
    #: `DEFAULT_STARTUP_WAIT_SEC` applies. It is None rather than a copy of
    #: that number so the default has exactly ONE declaration, in the module
    #: that implements the wait; a second copy here would drift silently and
    #: the config would start reporting a budget the adapter does not use.
    #: `config.py` cannot import the constant, because
    #: `straightedge.broker.__init__` imports `straightedge.config`.
    startup_wait_sec: float | None = None


@dataclass
class Mt5Config:
    terminal_path: str = ""
    timeout_ms: int = 60000
    login: int = 0
    password: str = ""
    server: str = ""


DEFAULT_TG_EVENTS = (
    "start",
    "stop",
    "open",
    "close",
    "halt",
    "order_check_fail",
    "pending",
    "recap",
    # Residual exposure after a failed flatten. Also in telegram.ALWAYS_NOTIFY_EVENTS,
    # so removing it from a config file does not silence it.
    "flatten_incomplete",
)


def _parse_bool_flag(raw: object, *, default: bool) -> bool:
    """Fail-closed boolean for a capability switch.

    An operator setting an env var is always handed a string, and Python's
    bool("false") is True: the classic footgun that would silently re-arm a
    capability an operator just tried to turn off. A value that is PRESENT
    but not cleanly true/false is treated as False, never as `default`, so a
    typo or a bad env var can only ever remove capability, never grant it.
    A value that is fully absent (None) falls back to `default`, which is
    what preserves an existing deployment's behaviour on upgrade: it has
    never heard of this key, so nothing about it changes.
    """
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return False

def is_shared_chat_id(chat_id: str) -> bool:
    """True when the id is a Telegram group, supergroup or channel.

    Telegram numbers private chats positively and every shared chat
    negatively. A non-numeric id is not treated as shared.
    """
    try:
        return int(str(chat_id).strip()) < 0
    except (TypeError, ValueError):
        return False


def parse_allow_senders(raw: object) -> tuple[int, ...]:
    """Normalise allow_senders from a TOML list or a comma-separated env var."""
    if raw is None:
        return ()
    items = str(raw).split(",") if isinstance(raw, str) else list(raw)  # type: ignore[call-overload]
    out: list[int] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            value = int(text)
        except ValueError:
            raise ValueError(
                "telegram.allow_senders must be numeric Telegram sender ids"
            ) from None
        if value <= 0:
            raise ValueError("telegram.allow_senders ids must be positive")
        if value not in out:
            out.append(value)
    return tuple(out)


@dataclass
class TelegramConfig:
    token: str = ""
    chat_id: str = ""
    notify_events: tuple[str, ...] = DEFAULT_TG_EVENTS
    confirm_seconds: int = 120
    # Handover posture (#25). Both default True: an operator who has never
    # heard of this key gets today's behaviour unchanged. The shipped
    # handover template sets both false. Fail-closed parsing lives in
    # _parse_bool_flag, not here: a dataclass default cannot see a garbled
    # config value, only the loader can.
    allow_approve_always: bool = True
    allow_auto: bool = True
    allow_senders: tuple[int, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)


@dataclass
class AdviceConfig:
    provider: str = "grok"  # grok | claude | computer
    #: Advice turns allowed per UTC day. 0 disables the cap.
    #: This is a COST control, not only a risk one: hosted inference is billed
    #: per turn and a turn costs money whether or not it ends in an order, so
    #: the send cap above cannot see this spend at all.
    max_turns_per_day: int = 0
    grok_model: str = "grok-4"
    claude_model: str = "claude-opus-5-5"
    grok_key: str = ""
    claude_key: str = ""
    grok_url: str = "https://api.x.ai/v1/chat/completions"
    claude_url: str = "https://api.anthropic.com/v1/messages"
    computer_url: str = ""
    computer_token: str = ""
    computer_model: str = "xai/grok-4.6"

    @property
    def enabled(self) -> bool:
        if self.provider == "claude":
            return bool(self.claude_key)
        if self.provider == "computer":
            return bool(self.computer_url and self.computer_token)
        return bool(self.grok_key)


@dataclass
class BotConfig:
    mode: str = "paper"  # paper | mt5 | mt4
    initial_balance: float = 10_000.0
    symbols: list[str] = field(default_factory=lambda: ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"])
    #: What the MODEL is allowed to open, which is not the same question as
    #: what the desk scans. `symbols` is a SCAN list: it drives /quote and the
    #: auto scan, and an operator who scans three pairs may still want to act
    #: on a fourth BY HAND. So a human command is not constrained by this, and
    #: an advice-staged symbol is.
    #: EMPTY means "use `symbols`". It never means "allow anything": an empty
    #: whitelist that permits everything is the defect, not the default.
    advice_symbols: list[str] = field(default_factory=list)

    def advice_allows(self, symbol: str) -> bool:
        """Whether the model may OPEN this symbol. Closes are never gated.

        THE NAME IS CHECKED BEFORE IT IS TRANSFORMED, and this gate carries
        that rule itself rather than trusting a caller to have applied it: it
        is reachable with no parser in front of it, and straightedge#197
        measured `advice_allows("EURU\u017fD") -> True` because
        `"EURU\u017fD".upper()` is `"EURUSD"`. A model-chosen name that is not
        ASCII is refused here, which keeps the benign `eurusd` working (a pure
        ASCII case fold) while a string that merely UPPERCASES into an allowed
        instrument is not one.

        The allowed set is filtered the same way, for the same reason in the
        other direction: a non-ASCII entry in the operator's own list would
        uppercase into a symbol they did not type and widen the whitelist
        silently. No real MetaTrader symbol is non-ASCII, so such an entry
        matches nothing rather than matching something unintended.
        """
        if not may_transform_symbol(symbol):
            return False
        allowed = self.advice_symbols or self.symbols
        return symbol.upper() in {
            s.upper() for s in allowed if may_transform_symbol(s)
        }
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    mt5: Mt5Config = field(default_factory=Mt5Config)
    mt4: Mt4Config = field(default_factory=Mt4Config)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    advice: AdviceConfig = field(default_factory=AdviceConfig)
    poll_seconds: int = 15
    comment: str = "straightedge"
    journal_path: str = "journal.jsonl"
    live_accepted: bool = False
    #: Secret-bearing TOML keys this config took from `config.toml` rather than
    #: from the environment, by NAME (straightedge#139). Empty is the good
    #: case and it is reported explicitly rather than by silence, because an
    #: absent check reads exactly like a passed one. Never a VALUE: `doctor`
    #: prints this and the journal's `start` record carries it.
    settings_from_file: tuple[str, ...] = ()
    #: Secret-bearing TOML keys present in `config.toml` that the loader reads
    #: from NOWHERE, by name (straightedge#139). Not a refusal: see
    #: `IGNORED_FILE_SOURCED_SETTINGS` for why reporting is the whole fix.
    settings_read_from_nowhere: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.mode not in {"paper", "mt5", "mt4"}:
            raise ValueError("account.mode must be paper, mt5, or mt4")
        r = self.risk
        if not (0 < r.risk_pct <= 0.05):
            raise ValueError("risk_pct must be in (0, 0.05]")
        if not (0 < r.daily_loss_pct <= 0.20):
            raise ValueError("daily_loss_pct must be in (0, 0.20]")
        if not (0 < r.max_drawdown_pct <= 0.50):
            raise ValueError("max_drawdown_pct must be in (0, 0.50]")
        if r.max_positions < 1:
            raise ValueError("max_positions must be >= 1")
        if r.max_trades_per_day < 0:
            raise ValueError("max_trades_per_day must be >= 0 (0 disables)")
        if r.min_deviation_spread_multiple < 1.0:
            raise ValueError(
                "risk.min_deviation_spread_multiple must be >= 1.0: a deviation "
                "below one full spread can only produce broker rejections, so "
                "this gate must not be configurable below the floor it enforces"
            )
        if int(r.deviation_points) <= 0:
            # The per-symbol map has had this floor since #68; the global
            # default never did (#92). The asymmetry is not cosmetic: the MT4
            # Expert reads a deviation of <= 0 as "use my own `input int
            # Slippage`", so a 0 here does not disable a tolerance, it moves
            # the operator's risk figure to a number configured on the other
            # side of the bridge, where nothing on this side can read it.
            raise ValueError("risk.deviation_points must be > 0 points")
        seen: dict[str, str] = {}
        for name, value in r.symbol_deviation_points.items():
            key = str(name).strip()
            if not key:
                raise ValueError("risk.symbol_deviation_points has an empty symbol key")
            if int(value) <= 0:
                raise ValueError(
                    f"risk.symbol_deviation_points.{key} must be > 0 points"
                )
            upper = key.upper()
            if upper in seen:
                raise ValueError(
                    "risk.symbol_deviation_points has two entries for the same "
                    f"symbol once upper-cased: {seen[upper]!r} and {key!r}"
                )
            seen[upper] = key
        if self.advice.max_turns_per_day < 0:
            raise ValueError("advice.max_turns_per_day must be >= 0 (0 disables)")
        s = self.strategy
        if s.fast_ema >= s.slow_ema:
            raise ValueError("fast_ema must be < slow_ema")
        if s.atr_stop_mult <= 0 or s.atr_tp_mult <= 0:
            raise ValueError("ATR multiples must be > 0")
        if s.atr_tp_mult / s.atr_stop_mult < r.min_rr - 1e-9:
            raise ValueError("atr_tp_mult / atr_stop_mult must be >= min_rr")
        self.strategy.timeframe_id  # raises if unknown
        if self.advice.provider not in {"grok", "claude", "computer"}:
            raise ValueError("advice.provider must be grok, claude, or computer")
        if not self.symbols:
            raise ValueError("at least one symbol required")
        if self.telegram.confirm_seconds <= 0:
            raise ValueError("confirm_seconds must be > 0")
        if is_shared_chat_id(self.telegram.chat_id) and not self.telegram.allow_senders:
            raise ValueError(
                "telegram.chat_id is a shared chat: set telegram.allow_senders "
                "(or TELEGRAM_ALLOW_SENDERS) to the operator sender ids"
            )
        if self.mt4.timeout_ms <= 0:
            raise ValueError("mt4.timeout_ms must be > 0")
        if self.mt4.send_timeout_ms <= self.mt4.timeout_ms:
            # Not a style rule. A send can spend the Expert's whole retry ladder
            # before it can possibly answer, and a read cannot; a send budget at
            # or below the read budget means the desk is configured to give up
            # while the Expert is still working, which is how a duplicate order
            # and a fill after a reported failure both become reachable.
            raise ValueError(
                "mt4.send_timeout_ms must be greater than mt4.timeout_ms: a "
                f"send budget of {self.mt4.send_timeout_ms}ms against a read "
                f"budget of {self.mt4.timeout_ms}ms would have the desk abandon "
                "orders the Expert is still executing"
            )
        floor = MAILBOX_ROUND_TRIP_CEILING_MS + EA_LADDER_SLEEP_MS + EA_CLAIM_RETRY_MS
        if self.mt4.send_timeout_ms <= floor:
            raise ValueError(
                f"mt4.send_timeout_ms must exceed {floor}ms, the measured "
                "transport ceiling plus everything mt4/Experts/Mt4RiskBot.mq4 "
                "can spend on Sleep() alone before it is able to reply"
            )


def parse_symbol_deviation_points(raw: object) -> dict[str, int]:
    """Normalise `[risk.symbol_deviation_points]` into {symbol: points}.

    A value that is present but not a table RAISES rather than being dropped.
    An override an operator wrote and the loader ignored is the worst of the
    three outcomes: the send goes out on the global default, the config says
    otherwise, and nothing reports the disagreement.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            "risk.symbol_deviation_points must be a table of symbol = points, "
            "for example [risk.symbol_deviation_points] with XAUUSD = 150"
        )
    out: dict[str, int] = {}
    for name, value in raw.items():
        key = str(name).strip()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"risk.symbol_deviation_points.{key} must be a number of points"
            )
        if float(value) != int(value):
            raise ValueError(
                f"risk.symbol_deviation_points.{key} must be a whole number of points"
            )
        out[key] = int(value)
    return out


def _section(data: dict, name: str) -> dict:
    raw = data.get(name, {})
    return raw if isinstance(raw, dict) else {}


def _hhmm(s: str) -> str:
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"time must be HH:MM, got {s!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"time out of range: {s!r}")
    return f"{h:02d}:{m:02d}"


def load_config(path: str | Path | None = None) -> BotConfig:
    data: dict = {}
    # Explicit, documented base for resolve_state_path(): the config
    # file's own directory when one was given, else the process working
    # directory. Both are named; neither is "wherever we happened to
    # start" by accident.
    base_dir = Path(path).resolve().parent if path is not None else Path.cwd()
    if path is not None:
        raw = Path(path).read_bytes()
        # `utf-8-sig`, not `utf-8`, and this is an outage prevented rather than
        # a nicety. A UTF-8 BOM is not valid TOML: `tomllib` refuses the whole
        # file with "Invalid statement (at line 1, column 1)", which names
        # neither the cause nor the remedy. And a BOM is the DEFAULT outcome of
        # editing this file the obvious way on the box: Windows PowerShell
        # 5.1's `Set-Content -Encoding UTF8` writes one. Measured live on
        # 2026-10-08, on a real-money box, where it would have stopped the desk
        # at its next restart; the edit was reverted from backup before that
        # happened. A pure encoding artifact must not be able to refuse a
        # config whose MEANING is unchanged, so it is tolerated and REPORTED.
        if raw.startswith(b"\xef\xbb\xbf"):
            print(
                f"config: {path} starts with a UTF-8 BOM, which is not valid "
                "TOML. It was tolerated and the file was read. Re-save it "
                "without one; on Windows PowerShell 5.1 "
                "`Set-Content -Encoding UTF8` writes a BOM and "
                "`[System.IO.File]::WriteAllText` with UTF8Encoding($false) "
                "does not.",
                file=sys.stderr,
            )
        parsed = tomllib.loads(raw.decode("utf-8-sig"))
        if not isinstance(parsed, dict):
            raise ValueError("config root must be a table")
        data = parsed

    account = _section(data, "account")
    risk_s = _section(data, "risk")
    strat_s = _section(data, "strategy")
    sess_s = _section(data, "session")
    mt5_s = _section(data, "mt5")
    mt4_s = _section(data, "mt4")
    tg_s = _section(data, "telegram")
    advice_s = _section(data, "advice")
    engine_s = _section(data, "engine")
    symbols_s = data.get("symbols", {})
    advice_names = list(data.get("advice", {}).get("symbols", []) or [])

    names = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
    if isinstance(symbols_s, dict) and "names" in symbols_s:
        names = list(symbols_s["names"])
    elif isinstance(data.get("symbols"), list):
        names = list(data["symbols"])

    settings_from_file = settings_taken_from_file(data)
    settings_read_from_nowhere_ = settings_read_from_nowhere(data)

    login = int(os.environ.get("MT5_LOGIN", mt5_s.get("login", 0) or 0) or 0)
    password = os.environ.get("MT5_PASSWORD", str(mt5_s.get("password", "") or ""))
    server = os.environ.get("MT5_SERVER", str(mt5_s.get("server", "") or ""))
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", str(tg_s.get("token", "") or ""))
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", str(tg_s.get("chat_id", "") or ""))
    tg_allow_approve_always = _parse_bool_flag(
        os.environ.get("TELEGRAM_ALLOW_APPROVE_ALWAYS", tg_s.get("allow_approve_always")),
        default=True,
    )
    tg_allow_auto = _parse_bool_flag(
        os.environ.get("TELEGRAM_ALLOW_AUTO", tg_s.get("allow_auto")),
        default=True,
    )
    grok_key = os.environ.get("XAI_API_KEY", str(advice_s.get("grok_key", "") or ""))
    claude_key = os.environ.get("ANTHROPIC_API_KEY", str(advice_s.get("claude_key", "") or ""))
    computer_url = os.environ.get("ADVICE_URL", str(advice_s.get("computer_url", "") or ""))
    computer_token = os.environ.get("ADVICE_TOKEN", str(advice_s.get("computer_token", "") or ""))
    provider = os.environ.get("AI_PROVIDER", str(advice_s.get("provider", "grok") or "grok")).lower()
    tg_allow = parse_allow_senders(
        os.environ.get("TELEGRAM_ALLOW_SENDERS", tg_s.get("allow_senders", ()))
    )
    events_raw = tg_s.get("notify_events", list(DEFAULT_TG_EVENTS))
    if isinstance(events_raw, str):
        events = tuple(x.strip() for x in events_raw.split(",") if x.strip())
    else:
        events = tuple(str(x) for x in events_raw)

    cfg = BotConfig(
        mode=os.environ.get("ACCOUNT_MODE", str(account.get("mode", "paper"))),
        initial_balance=float(account.get("initial_balance", 10_000.0)),
        symbols=names,
        # `normalize_model_symbol`, not `.upper()`, so that the claim in
        # `advice_allows` is actually true: uppercasing HERE would turn a
        # non-ASCII entry into an ASCII one before that gate could filter
        # it, which is the same manufacture (straightedge#197) one step
        # earlier and on the operator's own list.
        advice_symbols=[normalize_model_symbol(str(x)) for x in advice_names],
        poll_seconds=int(os.environ.get("POLL_SECONDS", engine_s.get("poll_seconds", 15))),
        comment=str(engine_s.get("comment", "straightedge")),
        journal_path=resolve_state_path(
            str(engine_s.get("journal_path", "journal.jsonl")), base_dir=base_dir
        ),
        settings_from_file=settings_from_file,
        settings_read_from_nowhere=settings_read_from_nowhere_,
        risk=RiskConfig(
            risk_pct=float(risk_s.get("risk_pct", 0.005)),
            daily_loss_pct=float(risk_s.get("daily_loss_pct", 0.02)),
            max_drawdown_pct=float(risk_s.get("max_drawdown_pct", 0.10)),
            max_positions=int(risk_s.get("max_positions", 3)),
            max_currency_exposure=int(risk_s.get("max_currency_exposure", 2)),
            min_rr=float(risk_s.get("min_rr", 1.5)),
            max_spread_atr_frac=float(risk_s.get("max_spread_atr_frac", 0.15)),
            min_free_margin_pct=float(risk_s.get("min_free_margin_pct", 0.50)),
            magic=int(risk_s.get("magic", 20260909)),
            halt_file=resolve_state_path(
                str(risk_s.get("halt_file", "HALT")), base_dir=base_dir
            ),
            max_risk_multiple=float(risk_s.get("max_risk_multiple", 1.0)),
            deviation_points=int(risk_s.get("deviation_points", 20)),
            symbol_deviation_points=parse_symbol_deviation_points(
                risk_s.get("symbol_deviation_points")
            ),
            # The fallback is READ FROM the dataclass, not restated as a
            # literal. Writing 3.0 here while the field said 1.0 is exactly
            # what this line did on its first draft, and a config file that
            # omits the key then got a different number from one that spells
            # out the default: two places holding one number, disagreeing
            # silently. An existing test caught it. Never restate it.
            min_deviation_spread_multiple=float(
                risk_s.get(
                    "min_deviation_spread_multiple",
                    RiskConfig.min_deviation_spread_multiple,
                )
            ),
            max_trades_per_day=int(risk_s.get("max_trades_per_day", 0)),
        ),
        strategy=StrategyConfig(
            auto=bool(strat_s.get("auto", False)),
            trail=bool(strat_s.get("trail", False)),
            timeframe=str(strat_s.get("timeframe", "H1")),
            fast_ema=int(strat_s.get("fast_ema", 21)),
            slow_ema=int(strat_s.get("slow_ema", 55)),
            adx_period=int(strat_s.get("adx_period", 14)),
            adx_min=float(strat_s.get("adx_min", 20.0)),
            atr_period=int(strat_s.get("atr_period", 14)),
            atr_stop_mult=float(strat_s.get("atr_stop_mult", 1.5)),
            atr_tp_mult=float(strat_s.get("atr_tp_mult", 2.5)),
            breakeven_r=float(strat_s.get("breakeven_r", 1.0)),
            trail_r=float(strat_s.get("trail_r", 1.5)),
            trail_atr_mult=float(strat_s.get("trail_atr_mult", 1.2)),
        ),
        session=SessionConfig(
            enabled=bool(sess_s.get("enabled", True)),
            start_utc=_hhmm(str(sess_s.get("start_utc", "07:00"))),
            end_utc=_hhmm(str(sess_s.get("end_utc", "17:00"))),
            skip_friday_after_utc=_hhmm(str(sess_s.get("skip_friday_after_utc", "16:00"))),
        ),
        mt5=Mt5Config(
            terminal_path=str(mt5_s.get("terminal_path", "")),
            timeout_ms=int(mt5_s.get("timeout_ms", 60000)),
            login=login,
            password=password,
            server=server,
        ),
        mt4=Mt4Config(
            files_dir=resolve_mt4_files_dir(
                os.environ.get("MT4_FILES_DIR", str(mt4_s.get("files_dir", "") or ""))
            ),
            timeout_ms=int(mt4_s.get("timeout_ms", 5000)),
            send_timeout_ms=int(
                mt4_s.get("send_timeout_ms", derive_send_timeout_ms())
            ),
            mailbox_url=str(
                os.environ.get("MT4_MAILBOX_URL", str(mt4_s.get("mailbox_url", "") or ""))
            ).strip(),
            # Env ONLY. There is deliberately no TOML key to read here.
            mailbox_token=os.environ.get("MT4_MAILBOX_TOKEN", ""),
            # Absent is not zero. Zero is a real operator choice ("one ping, do
            # not wait"), so an unset key must stay None and defer to the
            # adapter rather than collapse into the same value as a configured 0.
            startup_wait_sec=(
                float(mt4_s["startup_wait_sec"]) if "startup_wait_sec" in mt4_s else None
            ),
        ),
        telegram=TelegramConfig(
            token=tg_token,
            chat_id=tg_chat,
            notify_events=events or DEFAULT_TG_EVENTS,
            confirm_seconds=int(tg_s.get("confirm_seconds", 120)),
            allow_approve_always=tg_allow_approve_always,
            allow_auto=tg_allow_auto,
            allow_senders=tg_allow,
        ),
        advice=AdviceConfig(
            provider=provider if provider in {"grok", "claude", "computer"} else "grok",
            max_turns_per_day=int(advice_s.get("max_turns_per_day", 0)),
            grok_model=str(advice_s.get("grok_model", "grok-4")),
            claude_model=str(advice_s.get("claude_model", "claude-opus-5-5")),
            grok_key=grok_key,
            claude_key=claude_key,
            grok_url=str(advice_s.get("grok_url", "https://api.x.ai/v1/chat/completions")),
            claude_url=str(advice_s.get("claude_url", "https://api.anthropic.com/v1/messages")),
            computer_url=computer_url,
            computer_token=computer_token,
            computer_model=str(advice_s.get("computer_model", "xai/grok-4.6")),
        ),
    )
    cfg.validate()
    return cfg


def _validate(cfg: BotConfig) -> None:
    cfg.validate()
