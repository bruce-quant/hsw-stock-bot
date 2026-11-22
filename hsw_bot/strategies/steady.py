"""Strategy A, "Steady": a monthly rotation between funds. Holds for weeks to months.

The idea in plain words:

1. Once a month, score every fund by how much it went up over the last
   3, 6 and 12 months (the average of the three returns). This is "momentum":
   funds that have been strong tend to stay strong for a while.
2. Trend filter ("absolute momentum"): if the stock market fund (SPY) scored
   WORSE than a short term Treasury fund (SHY), the trend is down. Then we
   hold only SHY and wait.
3. Otherwise hold the 3 best scored funds, one third of the account each.

The decision is made on the close of the FIRST trading day of each month and
filled at the next open. Between those days nothing changes, unless an order
could not fill; then the bot simply tries again the next day.

Why the first day of the month and not the last? To know that today is the
last day of a month, you must know tomorrow's date. That is a (small) peek at
the future. The first day of a month is known on the day itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from hsw_bot.config import ConfigError
from hsw_bot.data import Bars
from hsw_bot.strategies.base import Day, Entry, Exit, Position, Strategy, sort_entries


@dataclass(frozen=True)
class SteadyParams:
    safe_asset: str
    market_symbol: str
    momentum_days: tuple[int, ...]
    top_n: int

    def __post_init__(self) -> None:
        if not self.momentum_days or min(self.momentum_days) < 1:
            raise ConfigError("momentum_days needs one or more whole numbers above 0")
        if self.top_n < 1:
            raise ConfigError("top_n must be at least 1")


class Steady(Strategy):
    name = "steady"
    title = "Steady: monthly fund rotation"
    Params = SteadyParams
    params: SteadyParams
    random_test_note = (
        "For Steady this test leans against it on purpose: the months it waits in the "
        "Treasury fund count as trades with small returns. Its value is the smaller drops, "
        "which is why the Sharpe ratio and the worst drop are shown too."
    )

    def symbols(self) -> tuple[str, ...]:
        extra = [
            s for s in (self.params.safe_asset, self.params.market_symbol) if s not in self.universe
        ]
        return (*self.universe, *extra)

    def tradable(self) -> tuple[str, ...]:
        return (*self.universe, self.params.safe_asset)

    def warmup_days(self) -> int:
        return max(self.params.momentum_days) + 1

    def indicators(self, bars: Bars) -> dict[str, pd.DataFrame]:
        close = bars.close
        returns = [close / close.shift(n) - 1 for n in self.params.momentum_days]
        total = returns[0]
        for more in returns[1:]:
            total = total + more
        score = total / len(returns)
        return {"momentum": score, "pick": self._monthly_picks(score)}

    def _monthly_picks(self, score: pd.DataFrame) -> pd.DataFrame:
        """True where a fund is one of this month's picks.

        The picks are chosen on the first trading day of each month and then
        kept for the rest of the month. Rows before the first valid decision
        are all False (we hold nothing until we know enough).
        """
        dates = pd.DatetimeIndex(score.index)
        month = dates.month.to_numpy()
        # The first row has no earlier day, so we cannot tell if it starts a month.
        first_of_month = np.concatenate([[False], month[1:] != month[:-1]])
        columns = list(dict.fromkeys([*self.universe, self.params.safe_asset]))
        picked = np.zeros((len(dates), len(columns)), dtype=bool)
        current: list[str] = []
        for i in range(len(dates)):
            if first_of_month[i]:
                current = self._choose(score.iloc[i], current)
            for symbol in current:
                picked[i, columns.index(symbol)] = True
        return pd.DataFrame(picked, index=dates, columns=columns)

    def _choose(self, score: pd.Series, current: list[str]) -> list[str]:
        """This month's picks from today's scores (keep last month's if data is missing)."""
        p = self.params
        market, safe = score.get(p.market_symbol), score.get(p.safe_asset)
        if market is None or safe is None or pd.isna(market) or pd.isna(safe):
            return current
        if market <= safe:
            return [p.safe_asset]
        ranked = score.reindex(list(self.universe)).dropna()
        ranked = ranked.sort_values(ascending=False, kind="mergesort")
        return [str(s) for s in ranked.index[: p.top_n]]

    def exits(self, day: Day, positions: Mapping[str, Position]) -> list[Exit]:
        picks = day.today("pick")
        risk_off = bool(picks.get(self.params.safe_asset, False))
        exits = []
        for symbol in positions:
            if bool(picks.get(symbol, False)):
                continue
            if symbol == self.params.safe_asset:
                reason = "trend filter: the market trend turned up again"
            elif risk_off:
                reason = "trend filter: the market trend turned down, moving to the safe fund"
            else:
                reason = f"rotation: no longer one of the top {self.params.top_n} funds"
            exits.append(Exit(symbol, reason))
        return exits

    def entries(self, day: Day, positions: Mapping[str, Position]) -> list[Entry]:
        picks = day.today("pick")
        safe = self.params.safe_asset
        wanted = [str(s) for s, on in picks.items() if bool(on) and s not in positions]
        entries = []
        for symbol in wanted:
            score = day.value("momentum", symbol)
            score = score if np.isfinite(score) else 0.0
            if symbol == safe:
                entries.append(
                    Entry(symbol, score, "trend filter: market trend is down", weight=1.0)
                )
            else:
                reason = f"one of the top {self.params.top_n} funds by 3/6/12 month momentum"
                entries.append(Entry(symbol, score, reason, weight=1 / self.params.top_n))
        return sort_entries(entries)
