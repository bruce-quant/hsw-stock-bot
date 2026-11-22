"""YOUR strategy. Copy this file, rename it, and make it your own.

Read this file top to bottom once. Every part is explained.
The full walk through is in docs/MAKE_YOUR_OWN.md.

What is here right now is a small WORKING EXAMPLE, so you can run it and see
the whole loop work before you change anything:

    "Buy a breakout: the close is above its 100 day average and is the highest
     close of the last 50 days. Sell when the close drops below its 50 day
     average, or after 40 trading days."

It is only an example. It has NOT been through our stress tests. Do not trade
it with real money as it is.

Try it:

    hsw-bot backtest --strategy my_strategy

Three rules keep a strategy honest. The guard tests in tests/ check them for
every strategy in the registry, including this one:

1. No peeking. On day t, use only data from day t and earlier.
   Rolling windows (``.rolling(n)``) and ``.shift(n)`` with n >= 0 are fine.
   ``.shift(-1)`` reads tomorrow. Never use it.
2. No sizing here. Return WHAT to buy and how risky it is (the stop distance).
   The planner decides HOW MUCH, the same way for every strategy.
3. Every number comes from config.toml. Add your settings to the Params class
   below and to a [strategies.my_strategy] section in config.toml.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from hsw_bot.config import ConfigError
from hsw_bot.data import Bars
from hsw_bot.strategies.base import Day, Entry, Exit, Position, Strategy, sort_entries
from hsw_bot.strategies.swing import average_true_range


# --------------------------------------------------------------------------- 1. Settings
# Each field here must also appear in the [strategies.my_strategy] section of
# config.toml. The settings reader checks the types for you and refuses typos.
@dataclass(frozen=True)
class MyStrategyParams:
    trend_days: int  # the long average that defines "uptrend"
    breakout_days: int  # a close above the highest close of this many days is a breakout
    exit_days: int  # sell when the close falls below this average
    atr_days: int  # days in the "normal daily move" average
    stop_atr: float  # stop distance, in normal daily moves; used for sizing
    max_hold_days: int  # time stop

    # This runs when the settings are read. Refuse values that make no sense,
    # with a message that says how to fix them.
    def __post_init__(self) -> None:
        for name in ("trend_days", "breakout_days", "exit_days", "atr_days", "max_hold_days"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be at least 1")
        if self.stop_atr <= 0:
            raise ConfigError("stop_atr must be above 0")


# --------------------------------------------------------------------------- 2. The strategy
class MyStrategy(Strategy):
    # ``name`` must match the [strategies.NAME] section in config.toml.
    name = "my_strategy"
    title = "My strategy: an example breakout"
    Params = MyStrategyParams
    params: MyStrategyParams

    def warmup_days(self) -> int:
        """How many days of history the longest indicator needs."""
        p = self.params
        return max(p.trend_days, p.breakout_days, p.exit_days, p.atr_days + 1)

    # ----------------------------------------------------------------------- indicators
    def indicators(self, bars: Bars) -> dict[str, pd.DataFrame]:
        """Compute every number you need, for all days and all symbols at once.

        ``bars.close`` is a table: one row per day, one column per symbol.
        Work on whole tables like this. It is fast and it is easy to check.
        Return a dict of tables. Each name becomes something you can read later
        with ``day.today("name")`` or ``day.value("name", symbol)``.
        ("open", "high", "low" and "close" are always there for free.)
        """
        p = self.params
        close = bars.close
        trend = close.rolling(p.trend_days).mean()
        # The highest close of the PREVIOUS breakout_days days. shift(1) moves
        # the window back one day, so today's close is compared with the past.
        prior_high = close.shift(1).rolling(p.breakout_days).max()
        return {
            "trend": trend,
            "exit_line": close.rolling(p.exit_days).mean(),
            "atr": average_true_range(bars, p.atr_days),
            "strength": close / trend - 1,  # how far above the trend line, used to rank
            "breakout": (close > trend) & (close > prior_high),
        }

    # ----------------------------------------------------------------------- exits
    def exits(self, day: Day, positions: Mapping[str, Position]) -> list[Exit]:
        """Called every evening. Return the positions to sell at the next open.

        ``positions`` maps each symbol you hold to a Position with its
        ``entry_date``, ``signal_date``, ``entry_price`` and ``qty``.
        """
        p = self.params
        exits = []
        for symbol, position in positions.items():
            close = day.value("close", symbol)
            if not math.isfinite(close):
                continue  # no price today; look again tomorrow
            if close < day.value("exit_line", symbol):
                exits.append(Exit(symbol, f"close fell below the {p.exit_days} day average"))
            elif day.bars_since(position.entry_date) >= p.max_hold_days:
                exits.append(Exit(symbol, f"time: held for {p.max_hold_days} trading days"))
        return exits

    # ----------------------------------------------------------------------- entries
    def entries(self, day: Day, positions: Mapping[str, Position]) -> list[Entry]:
        """Called every evening. Return what you would like to buy, best first.

        Give each Entry a ``stop_distance`` (dollars per share you are willing
        to lose) or a ``weight`` (share of the account). The planner uses it to
        size the trade, and it never puts more than ``max_position_pct`` of the
        account in one stock.
        """
        breakout = day.today("breakout")
        entries = []
        for symbol, triggered in breakout.items():
            name = str(symbol)
            if not bool(triggered) or name in positions:
                continue
            strength = day.value("strength", name)
            stop_distance = self.params.stop_atr * day.value("atr", name)
            reason = f"breakout: highest close in {self.params.breakout_days} days"
            entries.append(Entry(name, strength, reason, stop_distance=stop_distance))
        return sort_entries(entries)


# --------------------------------------------------------------------------- 3. Register it
# Open hsw_bot/strategies/__init__.py and add your class to STRATEGIES.
# Then the backtest, the report, the bot and the guard tests all know about it.
