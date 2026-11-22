"""The shape every strategy has.

A strategy answers three questions, every evening, after the close:

1. ``indicators``: what numbers do I need? (averages, scores, signals)
2. ``exits``: which of my positions should I sell at the next open?
3. ``entries``: which new stocks should I buy at the next open, best first?

It never decides HOW MUCH to buy. Sizing lives in ``hsw_bot.planner``, the same
for every strategy. It never places orders either. The backtest engine and the
live bot both call these same methods, so a strategy cannot behave one way in
a backtest and another way with real money.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import pandas as pd

from hsw_bot.config import parse_params
from hsw_bot.data import Bars, to_float


class LookaheadError(Exception):
    """A strategy asked for data from the future. That would make a backtest lie."""


@dataclass(frozen=True)
class Position:
    """A stock we own.

    ``signal_date`` is the evening we decided to buy. ``entry_date`` is the
    next trading day, when the order filled at the open.
    """

    symbol: str
    qty: float
    entry_price: float
    entry_date: pd.Timestamp
    signal_date: pd.Timestamp
    entry_commission: float = 0.0

    @property
    def cost_basis(self) -> float:
        """What we paid in total, fees included."""
        return self.qty * self.entry_price + self.entry_commission


@dataclass(frozen=True)
class Entry:
    """A wish to buy ``symbol`` at the next open.

    Give at least one way to size it:

    * ``stop_distance``: dollars per share between today's close and the stop.
      The planner then risks a fixed share of the account on the trade.
    * ``weight``: the share of the account to put in, for example 1/3.

    ``score`` ranks the wishes. Higher scores get the free slots first.
    """

    symbol: str
    score: float
    reason: str
    stop_distance: float | None = None
    weight: float | None = None


@dataclass(frozen=True)
class Exit:
    """A wish to sell all of ``symbol`` at the next open, and why."""

    symbol: str
    reason: str


class Day:
    """What a strategy may look at on one evening: data up to and including today.

    ``Day`` only hands out today's values and values from earlier days. Asking
    for a later day raises ``LookaheadError``. That is one of our guard rails:
    a backtest that peeks at tomorrow looks wonderful and means nothing.
    """

    def __init__(self, index: int, dates: pd.DatetimeIndex, table: Mapping[str, pd.DataFrame]):
        self.index = index
        self.date = pd.Timestamp(dates[index])
        self._dates = dates
        self._table = table

    def today(self, name: str) -> pd.Series:
        """One indicator for every symbol today, e.g. ``day.today("close")``."""
        return self._frame(name).iloc[self.index]

    def value(self, name: str, symbol: str) -> float:
        """One indicator for one symbol today. NaN when there is no value."""
        return self._cell(name, self.index, symbol)

    def on(self, name: str, date: pd.Timestamp, symbol: str) -> float:
        """One indicator for one symbol on an earlier day (never a later one)."""
        if date > self.date:
            raise LookaheadError(
                f"Asked for {name} on {date.date()} while it is {self.date.date()}."
            )
        row = int(self._dates.get_indexer(pd.DatetimeIndex([date]))[0])
        if row < 0:
            raise KeyError(f"{date.date()} is not a trading day in the data.")
        return self._cell(name, row, symbol)

    def bars_since(self, date: pd.Timestamp) -> int:
        """Trading days from ``date`` to today, counting both. Same day = 1."""
        first = int(self._dates.searchsorted(date))
        return self.index - first + 1

    def _cell(self, name: str, row: int, symbol: str) -> float:
        frame = self._frame(name)
        column = int(frame.columns.get_indexer(pd.Index([symbol]))[0])
        if column < 0:
            return math.nan
        return to_float(frame.iat[row, column])

    def _frame(self, name: str) -> pd.DataFrame:
        if name not in self._table:
            known = ", ".join(sorted(self._table))
            raise KeyError(f"No indicator called {name!r}. Known: {known}.")
        return self._table[name]


class Strategy(ABC):
    """Base class for every trading idea. See ``my_strategy.py`` for a commented example."""

    name: ClassVar[str]
    """Short name, the same as the [strategies.NAME] section in config.toml."""

    title: ClassVar[str]
    """A friendly title for reports, e.g. "Steady: monthly fund rotation"."""

    Params: ClassVar[type[Any]]
    """The dataclass that holds this strategy's settings."""

    random_test_note: ClassVar[str] = ""
    """Anything a reader should know when judging this strategy's random entry test."""

    def __init__(self, params: Any, universe: tuple[str, ...]) -> None:
        self.params = params
        self.universe = universe

    @classmethod
    def from_settings(cls, params: Mapping[str, Any], universe: tuple[str, ...]) -> Strategy:
        """Build the strategy from its config.toml section."""
        return cls(parse_params(cls.Params, params, f"strategies.{cls.name}"), universe)

    def symbols(self) -> tuple[str, ...]:
        """Every symbol whose bars we need. Add helper symbols (like SPY) here."""
        return self.universe

    def tradable(self) -> tuple[str, ...]:
        """Every symbol this strategy may buy. The bot keeps strategies from sharing one."""
        return self.universe

    @abstractmethod
    def warmup_days(self) -> int:
        """How many trading days of history the indicators need before they are valid."""

    @abstractmethod
    def indicators(self, bars: Bars) -> dict[str, pd.DataFrame]:
        """Compute every number the strategy needs, for every day and symbol at once.

        Rule: the value on day t may only use bars from day t and earlier.
        Rolling windows (``.rolling(n)``) and ``.shift(n)`` with n >= 0 are safe.
        ``.shift(-1)`` looks at tomorrow and is NOT allowed.
        """

    @abstractmethod
    def exits(self, day: Day, positions: Mapping[str, Position]) -> list[Exit]:
        """Return the positions to sell at the next open."""

    @abstractmethod
    def entries(self, day: Day, positions: Mapping[str, Position]) -> list[Entry]:
        """Return new buys for the next open, best first. Skip symbols already held."""


def rank_score(table: pd.DataFrame) -> pd.Series:
    """Combine several columns into one score between 0 and 1.

    Each column is ranked from worst (near 0) to best (1). The score is the
    plain average of the ranks. Equal weights on purpose: we do not tune them.
    """
    if table.empty:
        return pd.Series(dtype="float64")
    return table.rank(pct=True).mean(axis=1)


def sort_entries(entries: list[Entry]) -> list[Entry]:
    """Best score first. Ties are broken by symbol, so results never depend on luck."""
    return sorted(entries, key=lambda e: (-e.score, e.symbol))
