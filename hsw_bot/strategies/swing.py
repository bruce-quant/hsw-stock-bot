"""Strategy B, "Swing": buy short dips inside strong uptrends. Holds for days to weeks.

The idea in plain words:

* Only look at stocks in a strong uptrend: the close is above the 200 day
  average, and the 50 day average is above the 200 day average.
* Wait for a dip: the close falls below the lower Bollinger band (the 20 day
  average minus 2 standard deviations). Strong stocks often bounce back
  after a sharp dip like this.
* When there are more dips than free slots, rank them with a simple score
  built from three different things, each ranked against the other dips:
    - momentum: the 6 month return (strong stocks tend to stay strong);
    - dip depth: how far below the 20 day average it closed (deeper dip,
      more room to bounce);
    - calm: a small average daily range compared with the price (calm stocks
      bounce more smoothly).
* Sell at the next open after the FIRST of these happens on a close:
    - stop: the close falls 3 "normal daily moves" (3 x ATR) below the close
      on the day we decided to buy;
    - rebound: the close is back above the 20 day average;
    - time: we have held for 20 trading days.

Every rule is checked on the daily close, and every order fills at the next
open. The live bot does exactly the same, so the backtest models the real
exits. There is no stop order resting at the broker during the day. That is
a deliberate choice; ``docs/SETUP_ALPACA.md`` explains why.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from hsw_bot.config import ConfigError
from hsw_bot.data import Bars
from hsw_bot.strategies.base import (
    Day,
    Entry,
    Exit,
    Position,
    Strategy,
    rank_score,
    sort_entries,
)


@dataclass(frozen=True)
class SwingParams:
    trend_fast_days: int
    trend_slow_days: int
    band_days: int
    band_std: float
    atr_days: int
    stop_atr: float
    max_hold_days: int
    momentum_days: int

    def __post_init__(self) -> None:
        if not 1 < self.trend_fast_days < self.trend_slow_days:
            raise ConfigError("trend_fast_days must be above 1 and below trend_slow_days")
        for name in ("band_days", "atr_days", "max_hold_days", "momentum_days"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be at least 1")
        if self.band_std <= 0 or self.stop_atr <= 0:
            raise ConfigError("band_std and stop_atr must be above 0")


def average_true_range(bars: Bars, days: int) -> pd.DataFrame:
    """The plain average of the last ``days`` true ranges.

    The true range of a day is the biggest of: high minus low, high minus the
    previous close, and the previous close minus low. It is the stock's
    "normal daily move" in dollars.

    We use a plain average on purpose. The other common version (Wilder's)
    remembers every bar back to the first one. Then the bot, which downloads
    only two years, would see slightly different numbers than the backtest.
    """
    prev_close = bars.close.shift(1)
    ranges = [bars.high - bars.low, (bars.high - prev_close).abs(), (bars.low - prev_close).abs()]
    true_range = pd.DataFrame(
        np.fmax(np.fmax(ranges[0].to_numpy(), ranges[1].to_numpy()), ranges[2].to_numpy()),
        index=bars.close.index,
        columns=bars.close.columns,
    )
    true_range = true_range.where(prev_close.notna() & bars.high.notna())
    return true_range.rolling(days).mean()


class Swing(Strategy):
    name = "swing"
    title = "Swing: dips in strong uptrends"
    Params = SwingParams
    params: SwingParams

    def warmup_days(self) -> int:
        p = self.params
        return max(p.trend_slow_days, p.band_days, p.atr_days + 1, p.momentum_days + 1)

    def indicators(self, bars: Bars) -> dict[str, pd.DataFrame]:
        p = self.params
        close = bars.close
        fast = close.rolling(p.trend_fast_days).mean()
        slow = close.rolling(p.trend_slow_days).mean()
        middle = close.rolling(p.band_days).mean()
        spread = close.rolling(p.band_days).std(ddof=0)
        spread = spread.where(spread > 0)
        lower = middle - p.band_std * spread
        atr = average_true_range(bars, p.atr_days)
        uptrend = (close > slow) & (fast > slow)
        return {
            "band_middle": middle,
            "zscore": (close - middle) / spread,
            "atr": atr,
            "atr_pct": atr / close,
            "momentum": close / close.shift(p.momentum_days) - 1,
            "setup": uptrend & (close < lower),
            "stop_level": close - p.stop_atr * atr,
        }

    def exits(self, day: Day, positions: Mapping[str, Position]) -> list[Exit]:
        p = self.params
        exits = []
        for symbol, position in positions.items():
            close = day.value("close", symbol)
            if not math.isfinite(close):
                continue  # no bar today; decide on the next close
            stop = day.on("stop_level", position.signal_date, symbol)
            if math.isfinite(stop) and close < stop:
                exits.append(
                    Exit(symbol, f"stop: close {close:.2f} fell below the stop {stop:.2f}")
                )
            elif close > day.value("band_middle", symbol):
                exits.append(
                    Exit(symbol, f"rebound: close is back above the {p.band_days} day average")
                )
            elif day.bars_since(position.entry_date) >= p.max_hold_days:
                exits.append(Exit(symbol, f"time: held for {p.max_hold_days} trading days"))
        return exits

    def entries(self, day: Day, positions: Mapping[str, Position]) -> list[Entry]:
        setup = day.today("setup")
        candidates = [str(s) for s, on in setup.items() if bool(on) and s not in positions]
        if not candidates:
            return []
        table = pd.DataFrame(
            {
                "momentum": day.today("momentum").reindex(candidates),
                "dip_depth": -day.today("zscore").reindex(candidates),
                "calm": -day.today("atr_pct").reindex(candidates),
            }
        ).dropna()
        scores = rank_score(table)
        entries = []
        for symbol, score in scores.items():
            name = str(symbol)
            z = day.value("zscore", name)
            days = self.params.band_days
            reason = f"dip in an uptrend: close {abs(z):.1f} std below the {days} day average"
            stop_distance = self.params.stop_atr * day.value("atr", name)
            entries.append(Entry(name, float(score), reason, stop_distance=stop_distance))
        return sort_entries(entries)
