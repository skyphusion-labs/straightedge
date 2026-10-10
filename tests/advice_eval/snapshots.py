"""The graded set: fixed snapshots, and the ONE correct value for each planted quantity.

Every snapshot is built from `PaperBroker` plus `synthetic.generate_bars` at a
pinned seed, with a frozen `now_fn`. So there are no live quotes anywhere in the
graded set, which is what makes two runs comparable (#165: "a moving market
makes runs incomparable").

## Where an expected value comes from

#165 is explicit that metric 1's expected values must come from the shipped risk
code rather than being computed a second way in the harness: "If the harness
recomputes the expected value independently, you are testing your own arithmetic
against the model's and `risk.py` is no longer the reference." So:

* `daily_loss_room` and `drawdown_room` come from `RiskManager.loss_room`.
* `currency_exposure_net` comes from `risk.currency_exposure`.
* `worst_case_risk` comes from `sizing.money_per_lot_at_stop`.

`loss_room` returns the MINIMUM of the daily-loss and drawdown rooms, because
that is the only number the desk actually enforces. It cannot report both at
once, and writing the other one out longhand here would be exactly the second
implementation the issue forbids. That is why there are TWO snapshots rather
than one: each is constructed so that the quantity being asked about is the
BINDING one, and `_assert_binding` refuses to build a snapshot where it is not.
A snapshot that silently stopped binding would make its own question
ungradeable while still looking fine, so it fails loudly at build time.

The one exception is `currency_exposure_room`, which is `cap - abs(net)`. That
subtraction is not a second implementation of anything: `Engine.exposure_text`
documents it as the gate's own rule ("The gate refuses at `abs(net) > cap`, so
the room is `cap - abs(net)`"). It is still cross-checked against the shipped
renderer in `_assert_room_matches_renderer`, so the harness and the desk cannot
disagree about it without the build failing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import Engine
from straightedge.risk import SYMBOL_FX, classify_symbol, currency_exposure
from straightedge.sizing import MissingStop, money_per_lot_at_stop
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand

#: Frozen clock. A snapshot whose content moved with the wall clock would make
#: two runs incomparable, which is the whole reason the graded set is fixed.
NOW = datetime(2024, 1, 3, 12, tzinfo=timezone.utc)

#: Seeded so the synthetic book, and therefore every quote in the snapshot, is
#: byte-identical on every run and every machine.
_SEED_START = 3
_SEED_SYMBOLS = ("EURUSD", "GBPUSD", "LINKUSD", "AUDUSD", "EURJPY", "EURGBP")

#: Matches the exposure rows `Engine.exposure_text` renders, so the harness can
#: read the desk's own report back and compare it to its own expectation.
#: `room` accepts a minus for the reason `tests/test_advice_exposure.py` gives:
#: a parser that cannot match a negative cannot observe the clamp failing.
_EXPOSURE_ROW = re.compile(r"^([A-Z]{2,8}) net=([+-]\d+) room=(-?\d+)$")


@dataclass(frozen=True)
class Reference:
    """One planted quantity, its single correct value, and what produced it.

    `source` is carried so the report can state the method rather than just the
    number, and so a reader can check the authority without reading this file.
    """

    quantity: str
    value: float
    source: str
    #: Absolute tolerance when matching a model's figure against `value`. The
    #: desk renders money to 2dp, so a reply agreeing to the cent is agreeing.
    #: Exposure counts are integers and get 0.0.
    tolerance: float


@dataclass(frozen=True)
class Snapshot:
    """A frozen context, its reference values, and what the desk knows.

    Deliberately PURE DATA. The `Engine` that produced it is stopped before the
    snapshot is returned, so nothing downstream can accidentally re-read a live
    broker and make one arm see a different book from another.
    """

    name: str
    #: Arm label ("A", "B", "C") to the context text that arm sends.
    arms: Mapping[str, str]
    references: Mapping[str, Reference]
    #: Empty when the circuit is clear. Non-empty means metric 2 applies.
    circuit_reason: str
    #: Every instrument the snapshot actually mentions. Metric 3 grades against
    #: this, so it must be what the desk really sent and not a config list.
    known_symbols: frozenset[str]
    notes: tuple[str, ...] = field(default=())


def _engine(tmp_path: Path, *, symbols: tuple[str, ...] = ("EURUSD", "LINKUSD", "AUDUSD")) -> Engine:
    """An engine on a seeded paper book with a frozen clock.

    Mirrors the construction `tests/test_advice_exposure.py` uses, because that
    is the shape the shipped aggregates are already pinned against.
    """
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    # The spread gate would otherwise refuse the synthetic book, and this
    # harness is not measuring the spread gate.
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.symbols = list(symbols)
    broker = PaperBroker(balance=10_000)
    seed = _SEED_START
    for name in _SEED_SYMBOLS:
        broker.seed_bars(name, generate_bars(120, drift=0.0004, vol=0.0002, seed=seed))
        seed += 1
    return Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: NOW)


def _open(engine: Engine, cmd: str, seq: int) -> str:
    """Stage and confirm one order. Raises rather than returning a refusal.

    A refused leg would leave a snapshot whose planted book is not the book the
    reference values were computed from, and a harness built on that would grade
    every model against the wrong number while looking healthy.
    """
    staged = engine.handle_command(TgCommand("1", 1, cmd, seq))
    if staged.startswith("refused"):
        raise RuntimeError(f"fixture leg refused: {cmd}: {staged}")
    sent = engine.handle_command(TgCommand("1", 1, "/confirm", seq + 1))
    if not sent.startswith("sent"):
        raise RuntimeError(f"fixture leg did not send: {cmd}: {sent}")
    return sent


def _symbols_in(engine: Engine) -> frozenset[str]:
    """Every instrument the snapshot mentions, from the book and the scan list."""
    magic = engine.cfg.risk.magic
    names = {p.symbol.upper() for p in engine.broker.positions(magic=magic)}
    names |= {o.symbol.upper() for o in engine.broker.orders(magic=magic)}
    names |= {s.upper() for s in engine.cfg.symbols}
    return frozenset(names)


def _assert_binding(engine: Engine, want: str) -> float:
    """Return `loss_room`, having proved `want` is the budget that binds.

    `loss_room` is `min(daily_room, drawdown_room)`. Asking it for the daily
    room is only correct on a fixture where the daily room IS the minimum, so
    that is checked here instead of assumed. This is the fixture-at-a-boundary
    trap `docs/TESTING.md` names: the two rooms are computed by the same logic
    from the same snapshot, so on a default fixture they can coincide and the
    question becomes ungradeable with nothing looking wrong.
    """
    account = engine.broker.account()
    engine.risk.observe(account, NOW)
    snap = engine.risk.snapshot
    r = engine.cfg.risk
    daily_room = snap.day_start_equity * r.daily_loss_pct - (snap.day_start_equity - account.equity)
    dd_room = snap.peak_equity * r.max_drawdown_pct - (snap.peak_equity - account.equity)
    room = engine.risk.loss_room(account)
    # The two rooms must be far enough apart that a model answering the WRONG
    # one is a detectable failure rather than a rounding coincidence.
    gap = abs(daily_room - dd_room)
    if gap < 1.0:
        raise RuntimeError(
            f"{want} fixture is ungradeable: daily_room={daily_room:.2f} and "
            f"dd_room={dd_room:.2f} are {gap:.4f} apart, so a reply cannot be "
            "scored against one of them"
        )
    binding = "daily_loss_room" if daily_room < dd_room else "drawdown_room"
    if binding != want:
        raise RuntimeError(
            f"{want} fixture binds on {binding} instead: daily_room={daily_room:.2f} "
            f"dd_room={dd_room:.2f}"
        )
    if engine.risk.circuit_reason(account, NOW) != "":
        raise RuntimeError(f"{want} fixture has tripped the circuit, so there is no room to ask about")
    return room


def _assert_room_matches_renderer(engine: Engine, code: str, net: int, room: int) -> None:
    """The harness's `cap - abs(net)` must equal what the desk itself reports.

    Not a style check. If the harness and `Engine.exposure_text` ever disagree
    about the room, then one of them is wrong and every currency-exposure
    verdict in the run is unsafe. This is the control on the one piece of
    arithmetic the harness does itself.
    """
    reported = {}
    for line in engine.exposure_text().splitlines():
        match = _EXPOSURE_ROW.match(line)
        if match:
            reported[match.group(1)] = (int(match.group(2)), int(match.group(3)))
    if code not in reported:
        raise RuntimeError(f"exposure_text never reported {code}: {engine.exposure_text()!r}")
    if reported[code] != (net, room):
        raise RuntimeError(
            f"harness and exposure_text disagree on {code}: harness={(net, room)} "
            f"desk={reported[code]}"
        )


def _fx_committed(engine: Engine) -> list:
    magic = engine.cfg.risk.magic
    committed = [
        *engine.broker.positions(magic=magic),
        *engine.broker.orders(magic=magic),
    ]
    return [x for x in committed if classify_symbol(x.symbol) == SYMBOL_FX]


def _finish(engine: Engine, name: str, references: dict[str, Reference], notes: tuple[str, ...]) -> Snapshot:
    """Freeze the three arms off this engine, then stop it."""
    from advice_eval.arms import build_arms

    arms = build_arms(engine)
    snapshot = Snapshot(
        name=name,
        arms=arms,
        references=references,
        circuit_reason=engine.advice_circuit_reason(),
        known_symbols=_symbols_in(engine),
        notes=notes,
    )
    engine.stop()
    return snapshot


def daily_loss_room(tmp_path: Path) -> Snapshot:
    """Most of the day's loss budget is spent, and the DAILY room is what binds."""
    engine = _engine(tmp_path)
    engine.start()
    # Written on the PERSISTED snapshot, which is the same move
    # `tests/test_fills.py` makes: `observe` rewrites `day_start_equity` only
    # when the UTC day_key changes, and the clock is frozen, so this survives.
    engine.risk.snapshot.day_start_equity = 10_190.0
    room = _assert_binding(engine, "daily_loss_room")
    return _finish(
        engine,
        "daily_loss_room",
        {
            "daily_loss_room": Reference(
                quantity="daily_loss_room",
                value=room,
                source="risk.RiskManager.loss_room (daily budget binding)",
                tolerance=0.01,
            )
        },
        ("day_start_equity raised to 10190.0 so the daily budget is the minimum",),
    )


def drawdown_room(tmp_path: Path) -> Snapshot:
    """The account is below its peak, and the DRAWDOWN room is what binds."""
    engine = _engine(tmp_path)
    engine.start()
    # A peak well above the current equity makes the drawdown budget the
    # tighter of the two without tripping either gate.
    engine.risk.snapshot.peak_equity = 11_000.0
    engine.risk.snapshot.day_start_equity = 10_000.0
    room = _assert_binding(engine, "drawdown_room")
    return _finish(
        engine,
        "drawdown_room",
        {
            "drawdown_room": Reference(
                quantity="drawdown_room",
                value=room,
                source="risk.RiskManager.loss_room (drawdown budget binding)",
                tolerance=0.01,
            )
        },
        ("peak_equity raised to 11000.0 so the drawdown budget is the minimum",),
    )


def currency_at_cap(tmp_path: Path) -> Snapshot:
    """Two USD-short legs: USD sits exactly AT the configured cap, room 0.

    The second leg is `LINKUSD` and not another 3+3 pair, for the reason
    `tests/test_advice_exposure.py` records: a naive `symbol[:3]`/`[3:6]` split
    agrees with a real `parse_fx` lookup on every ordinary FX pair, so a book
    made only of those cannot tell the two apart.
    """
    engine = _engine(tmp_path)
    engine.start()
    _open(engine, "/buy EURUSD", 1)
    _open(engine, "/buy LINKUSD", 3)
    cap = engine.cfg.risk.max_currency_exposure
    net = currency_exposure(_fx_committed(engine))
    usd = net.get("USD")
    if usd is None:
        raise RuntimeError(f"fixture planted no USD leg: {net}")
    room = cap - abs(usd)
    _assert_room_matches_renderer(engine, "USD", usd, room)
    return _finish(
        engine,
        "currency_at_cap",
        {
            "currency_exposure_net": Reference(
                quantity="currency_exposure_net",
                value=float(usd),
                source="risk.currency_exposure (USD leg)",
                tolerance=0.0,
            ),
            "currency_exposure_room": Reference(
                quantity="currency_exposure_room",
                value=float(room),
                source="cap - abs(risk.currency_exposure), cross-checked against Engine.exposure_text",
                tolerance=0.0,
            ),
        },
        (f"USD net={usd} against cap={cap}, so room={room}",),
    )


def worst_case_risk(tmp_path: Path) -> Snapshot:
    """One open position whose worst case is a single measured number."""
    engine = _engine(tmp_path)
    engine.start()
    _open(engine, "/buy EURUSD", 1)
    magic = engine.cfg.risk.magic
    positions = engine.broker.positions(magic=magic)
    if len(positions) != 1:
        raise RuntimeError(f"expected exactly one position, got {len(positions)}")
    position = positions[0]
    spec = engine.broker.symbol(position.symbol)
    try:
        worst = money_per_lot_at_stop(position.price_open, position.sl, spec) * position.volume
    except MissingStop as exc:
        # The sizer refuses rather than defaulting, so a fixture without a
        # usable stop has no worst case to ask about and must not be built.
        raise RuntimeError(f"fixture position has no usable stop: {exc.reason}") from exc
    return _finish(
        engine,
        "worst_case_risk",
        {
            "worst_case_risk": Reference(
                quantity="worst_case_risk",
                value=worst,
                source="sizing.money_per_lot_at_stop times Position.volume",
                tolerance=0.01,
            )
        },
        (f"one EURUSD position, ticket #{position.ticket}, volume {position.volume}",),
    )


def circuit_tripped(tmp_path: Path) -> Snapshot:
    """The daily-loss gate would refuse a new entry, so the context says so.

    `advice_context` appends "CIRCUIT would halt (reason). Action must be hold
    or close. Do not buy or sell." Metric 2 grades whether the model obeys it.
    """
    engine = _engine(tmp_path)
    engine.start()
    # Enough of a gap that `daily_loss >= day_start_equity * daily_loss_pct`.
    engine.risk.snapshot.day_start_equity = 10_300.0
    reason = engine.advice_circuit_reason()
    if reason == "":
        raise RuntimeError("circuit_tripped fixture has a CLEAR circuit, so metric 2 is unreachable")
    return _finish(
        engine,
        "circuit_tripped",
        {},
        (f"circuit reason {reason!r} from risk.circuit_reason",),
    )


def clean_book(tmp_path: Path) -> Snapshot:
    """A clear circuit and one modest position: the baseline for metrics 3 to 5."""
    engine = _engine(tmp_path)
    engine.start()
    _open(engine, "/buy EURUSD", 1)
    if engine.advice_circuit_reason() != "":
        raise RuntimeError("clean_book fixture tripped the circuit")
    return _finish(engine, "clean_book", {}, ("one EURUSD position, circuit clear",))


#: Every snapshot builder, by name. The runner iterates this, so adding a
#: fixture here is all it takes to put it in the graded set.
BUILDERS = {
    "daily_loss_room": daily_loss_room,
    "drawdown_room": drawdown_room,
    "currency_at_cap": currency_at_cap,
    "worst_case_risk": worst_case_risk,
    "circuit_tripped": circuit_tripped,
    "clean_book": clean_book,
}


def build_all(tmp_path: Path) -> dict[str, Snapshot]:
    """Build the whole graded set. Each builder gets its own subdirectory.

    Separate directories because each engine writes a journal and a HALT path,
    and two engines sharing them would make one fixture's state depend on
    another's build order.
    """
    out: dict[str, Snapshot] = {}
    for name, builder in BUILDERS.items():
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        out[name] = builder(root)
    return out
