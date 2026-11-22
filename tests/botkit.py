"""Helpers to run the real bot code against the fake broker and the fake market."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from hsw_bot.broker.base import BrokerPosition
from hsw_bot.broker.fake import FakeBroker
from hsw_bot.config import Config, StrategySettings, load_config
from hsw_bot.data import Bars, to_float
from hsw_bot.journal import Journal
from hsw_bot.runner import BotContext, Mode, RunReport, run_once
from hsw_bot.state import BotState, PositionRecord, save_state
from tests import synthetic

REPO = Path(__file__).resolve().parent.parent
MARKET_TZ = ZoneInfo("America/New_York")
EVENING_HOUR = 18
OLD_ENTRY_BARS = 40  # a position this old has passed its 20 day time stop
FAKE_ACCOUNT = "fake-account-1"


def _toml_like(params: Any) -> dict[str, Any]:
    """Dataclass params back into the plain types config.toml would hold."""
    return {k: list(v) if isinstance(v, tuple) else v for k, v in asdict(params).items()}


def make_config(tmp: Path, sleeves: dict[str, float] | None = None, **bot: Any) -> Config:
    """The real config.toml, pointed at the fake market and a temporary folder."""
    base = load_config(REPO / "config.toml")
    strategies = {
        "steady": StrategySettings(
            "steady",
            (*synthetic.FUNDS, synthetic.MARKET),
            synthetic.STRATEGY_RULES["steady"],
            _toml_like(synthetic.steady().params),
        ),
        "swing": StrategySettings(
            "swing",
            synthetic.STOCKS,
            synthetic.STRATEGY_RULES["swing"],
            _toml_like(synthetic.swing().params),
        ),
    }
    tmp.mkdir(parents=True, exist_ok=True)
    changes: dict[str, Any] = {
        "sleeves": sleeves or {"steady": 0.5, "swing": 0.5},
        "state_dir": tmp / "state",
        "journal_file": tmp / "logs" / "journal.csv",
        "log_file": tmp / "logs" / "bot.log",
        "kill_switch_file": tmp / "STOP",
        "ignore_symbols": (),
        "order_poll_seconds": 0.0,
    }
    changes.update(bot)
    settings = replace(base.bot, **changes)
    return replace(base, root=tmp, strategies=strategies, bot=settings)


def state_path(config: Config, mode: Mode = Mode.PAPER) -> Path:
    return config.bot.state_path(mode.value)


def evening(day: pd.Timestamp) -> datetime:
    """6 PM in New York on ``day``: after the close, the time the bot is meant to run."""
    return datetime(day.year, day.month, day.day, EVENING_HOUR, 0, tzinfo=MARKET_TZ)


def make_broker(
    day: pd.Timestamp, cash: float = 10_000.0, paper_calendar: bool = True
) -> FakeBroker:
    """A fake broker whose clock says it is the evening of ``day``.

    Its previous close balance is dated the trading day before ``day`` (the
    fake market has no holidays, so that is the previous weekday).
    """
    when = evening(day)
    previous = (day - pd.offsets.BDay(1)).date()
    return FakeBroker(
        cash=cash,
        now=lambda: when,
        session=day.date() if paper_calendar else None,
        balance_asof=previous if paper_calendar else None,
    )


def move_to(broker: FakeBroker, day: pd.Timestamp) -> None:
    """Set the fake broker's clock and calendar to the evening of ``day``."""
    when = evening(day)
    broker.now = lambda: when
    broker.session = day.date()
    broker.balance_asof = (day - pd.offsets.BDay(1)).date()


def next_open(broker: FakeBroker, bars: Bars, day: pd.Timestamp) -> pd.Timestamp:
    """Pretend the market opened on the day after ``day``: fill orders, value positions."""
    after = bars.dates[list(bars.dates).index(day) + 1]
    opens = {s: to_float(bars.open.at[after, s]) for s in bars.symbols}
    broker.open_session(opens, evening(after).replace(hour=9, minute=30))
    broker.mark({s: to_float(bars.close.at[after, s]) for s in bars.symbols})
    move_to(broker, after)
    return after


def seed(
    config: Config,
    broker: FakeBroker,
    bars: Bars,
    day: pd.Timestamp,
    holdings: dict[str, tuple[float, float]],
    *,
    sleeve: str = "swing",
    peak: float | None = None,
    write: bool = True,
) -> None:
    """Give the bot old positions: {symbol: (shares in the records, shares at the broker)}.

    They were bought ``OLD_ENTRY_BARS`` days ago, so a Swing time stop fires today.
    """
    index = list(bars.dates).index(day)
    entry, signal = bars.dates[index - OLD_ENTRY_BARS], bars.dates[index - OLD_ENTRY_BARS - 1]
    records = {}
    for symbol, (in_records, at_broker) in holdings.items():
        price = to_float(bars.close.at[day, symbol])
        if at_broker > 0:
            broker.positions[symbol] = BrokerPosition(symbol, at_broker, 100.0, at_broker * price)
            broker.cash -= at_broker * price
        if in_records > 0:
            records[symbol] = PositionRecord(
                sleeve, symbol, in_records, 100.0, f"{entry:%Y-%m-%d}", f"{signal:%Y-%m-%d}"
            )
    if write:
        equity = broker.get_account().equity
        state = BotState(account_id=FAKE_ACCOUNT, positions=records, peak_equity=peak or equity)
        save_state(state, state_path(config))


def run(
    config: Config,
    broker: FakeBroker,
    bars: Bars,
    day: pd.Timestamp,
    mode: Mode = Mode.PAPER,
    run_id: str = "test",
) -> RunReport:
    """One bot run on the fake market as it looked on the evening of ``day``."""
    journal = Journal(config.bot.journal_file, run_id, mode.value)
    ctx = BotContext(
        config=config,
        broker=broker,
        mode=mode,
        load_bars=lambda symbols, now: bars.select(symbols).until(day),
        journal=journal,
        state_path=state_path(config, mode),
        sleep=lambda seconds: None,
    )
    return run_once(ctx)


def journal_events(config: Config) -> list[str]:
    """The event column of the journal, in order."""
    path = config.bot.journal_file
    if not path.exists():
        return []
    frame = pd.read_csv(path)
    return [str(e) for e in frame["event"]]


def busy_day(bars: Bars) -> pd.Timestamp:
    """A day on which the fake Swing strategy wants to buy (so tests have orders to look at)."""
    swing = synthetic.swing()
    setup = swing.indicators(bars.select(swing.symbols()))["setup"]
    for day in setup.index[300:]:
        if bool(setup.loc[day].any()):
            return pd.Timestamp(day)
    raise AssertionError("the fake market has no Swing setups")
