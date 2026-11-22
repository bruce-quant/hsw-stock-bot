"""Measure results, build the "do nothing clever" baselines, and run the stress tests.

A backtest number on its own means little. We always ask three questions:

1. Did it beat simply buying and holding? We use two baselines:
   * SPY buy and hold: the whole market, the easy choice;
   * equal weight of the SAME list the strategy picks from. If a strategy
     only beats SPY because its list is full of winners (see "survivorship
     bias" in docs/HOW_WE_TRADE.md), this second baseline catches it.
2. Does it survive double costs?
3. Is it better than luck? The random entry test makes thousands of fake
   traders who buy random stocks on random days and hold them exactly as
   long as the strategy did. If those random traders often do as well, the
   strategy's picks carry no real information.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from hsw_bot.config import CostModel
from hsw_bot.data import Bars
from hsw_bot.engine import BacktestResult

TRADING_DAYS_PER_YEAR = 252
DAYS_PER_YEAR = 365.25
MONTHS_PER_YEAR = 12
MIN_YEARS_FOR_CAGR = 1.0  # a yearly growth rate from a few weeks is noise, so we do not show it
RANDOM_CHUNK = 100  # random traders built per batch, to keep memory small
MAX_RESAMPLE_ROUNDS = 1000
LOW_PERCENTILE, HIGH_PERCENTILE = 5, 95  # the middle 90% of the random traders


@dataclass(frozen=True)
class Metrics:
    """The scorecard for one equity curve over one stretch of time."""

    start: pd.Timestamp
    end: pd.Timestamp
    total_return: float
    cagr: float | None
    max_drawdown: float
    sharpe: float
    volatility: float
    trades: int | None = None
    win_rate: float | None = None
    trades_per_month: float | None = None
    avg_hold_days: float | None = None
    exposure: float | None = None


def curve_metrics(curve: pd.Series, base: float) -> Metrics:
    """Score an equity curve. ``base`` is the account value just before the curve starts.

    * CAGR: the steady yearly growth rate that turns ``base`` into the final value.
    * max drawdown: the worst fall from a high point to a later low point.
    * Sharpe: average daily return divided by its spread, scaled to a year.
      (Risk free rate taken as zero to keep it simple.) Above 1 is good.
    """
    if curve.empty or base <= 0:
        raise ValueError("Cannot score an empty equity curve.")
    values = curve.to_numpy(dtype="float64")
    total = values[-1] / base - 1
    start, end = pd.Timestamp(curve.index[0]), pd.Timestamp(curve.index[-1])
    years = max((end - start).days, 1) / DAYS_PER_YEAR
    cagr = (values[-1] / base) ** (1 / years) - 1 if years >= MIN_YEARS_FOR_CAGR else None
    peaks = np.maximum.accumulate(np.concatenate([[base], values]))
    drawdown = float(np.min(np.concatenate([[base], values]) / peaks - 1))
    daily = np.diff(np.concatenate([[base], values])) / np.concatenate([[base], values[:-1]])
    spread = float(np.std(daily, ddof=1)) if len(daily) > 1 else 0.0
    sharpe = (
        float(np.mean(daily)) / spread * math.sqrt(TRADING_DAYS_PER_YEAR) if spread > 0 else 0.0
    )
    volatility = spread * math.sqrt(TRADING_DAYS_PER_YEAR)
    return Metrics(start, end, float(total), cagr, drawdown, sharpe, volatility)


def slice_curve(
    curve: pd.Series,
    base: float,
    start: str | pd.Timestamp | None,
    end: str | pd.Timestamp | None = None,
) -> tuple[pd.Series, float]:
    """Part of an equity curve, plus the account value on the day before it starts."""
    first = 0 if start is None else int(curve.index.searchsorted(pd.Timestamp(start)))
    last = len(curve) if end is None else int(curve.index.searchsorted(pd.Timestamp(end), "right"))
    part = curve.iloc[first:last]
    before = float(curve.iloc[first - 1]) if first > 0 else base
    return part, before


def last_days(curve: pd.Series, base: float, days: int) -> tuple[pd.Series, float]:
    """The last ``days`` trading days of a curve, plus the value just before them."""
    first = max(len(curve) - days, 0)
    return slice_curve(curve, base, curve.index[first])


def result_metrics(
    result: BacktestResult,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
) -> Metrics:
    """Score a backtest, with the trade statistics for trades closed in the window."""
    curve, base = slice_curve(result.equity, result.starting_cash, start, end)
    scores = curve_metrics(curve, base)
    trades = [t for t in result.trades if scores.start <= t.exit_date <= scores.end]
    months = max((scores.end - scores.start).days, 1) / DAYS_PER_YEAR * MONTHS_PER_YEAR
    invested, _ = slice_curve(result.invested, 0.0, start, end)
    exposure = float((invested / curve).mean()) if len(curve) else 0.0
    return Metrics(
        start=scores.start,
        end=scores.end,
        total_return=scores.total_return,
        cagr=scores.cagr,
        max_drawdown=scores.max_drawdown,
        sharpe=scores.sharpe,
        volatility=scores.volatility,
        trades=len(trades),
        win_rate=sum(t.pnl > 0 for t in trades) / len(trades) if trades else None,
        trades_per_month=len(trades) / months,
        avg_hold_days=float(np.mean([t.bars_held for t in trades])) if trades else None,
        exposure=exposure,
    )


# ----------------------------------------------------------------------------- baselines


def buy_and_hold(
    bars: Bars,
    symbol: str,
    costs: CostModel,
    cash: float,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp | None = None,
) -> pd.Series:
    """Buy ``symbol`` at the open of the first day (paying the same costs) and never sell."""
    opens = bars.open[symbol].loc[pd.Timestamp(start) : end]
    closes = bars.close[symbol].ffill().loc[pd.Timestamp(start) : end]
    first_open = opens.dropna()
    if first_open.empty:
        raise ValueError(f"No prices for {symbol} in the backtest window.")
    entry_day = first_open.index[0]
    price = float(first_open.iloc[0]) * (1 + costs.slippage_fraction)
    spend = cash - costs.commission_per_order
    qty = spend / price
    equity = (closes * qty).where(closes.index >= entry_day, cash)
    return equity.rename(f"{symbol} buy and hold")


def equal_weight(
    bars: Bars,
    symbols: Sequence[str],
    costs: CostModel,
    cash: float,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp | None = None,
) -> pd.Series:
    """Equal weight in every symbol of a list, topped back up to equal once a month.

    A stock joins at the first monthly top up after its first trading day. The
    top ups pay the same slippage as the strategies, on the dollars traded.
    """
    part = bars.select(symbols)
    dates = part.dates[(part.dates >= pd.Timestamp(start))]
    if end is not None:
        dates = dates[dates <= pd.Timestamp(end)]
    opens = part.open.loc[dates].to_numpy(dtype="float64")
    marks = part.close.ffill().loc[dates].to_numpy(dtype="float64")
    months = dates.month.to_numpy()
    units = np.zeros(opens.shape[1])
    spare = cash  # dollars not invested yet
    values: list[float] = []
    for i in range(len(dates)):
        if i == 0 or months[i] != months[i - 1]:
            spare, units = _top_up(units, spare, opens[i], costs)
        held = np.nansum(units * np.nan_to_num(marks[i], nan=0.0))
        values.append(spare + float(held))
    return pd.Series(values, index=dates, name="equal weight, same list")


def _top_up(
    units: np.ndarray, spare: float, opens: np.ndarray, costs: CostModel
) -> tuple[float, np.ndarray]:
    """Rebalance to equal dollars in every symbol that has an open price today.

    A symbol without a price today (not listed yet, or a gap in the data)
    keeps whatever it holds until a later top up.
    """
    tradable = np.isfinite(opens) & (opens > 0)
    count = int(tradable.sum())
    if count == 0:
        return spare, units
    pot = spare + float(np.sum(units[tradable] * opens[tradable]))
    traded = float(np.sum(np.abs(pot / count - units[tradable] * opens[tradable])))
    pot -= traded * costs.slippage_fraction
    new_units = units.copy()
    new_units[tradable] = pot / count / opens[tradable]
    return 0.0, new_units


def blend(curves: Sequence[tuple[pd.Series, float]], base: float) -> pd.Series:
    """Several equity curves mixed in fixed shares, put back to those shares every day.

    This is how the bot splits one account between strategies. Any share left
    over (when the shares add up to less than 1) sits in cash at no return.
    """
    index = pd.DatetimeIndex(curves[0][0].index)
    mixed = np.zeros(len(index))
    for curve, share in curves:
        values = curve.reindex(index).ffill().to_numpy(dtype="float64")
        previous = np.concatenate([[base], values[:-1]])
        mixed += share * (values / previous - 1)
    return pd.Series(base * np.cumprod(1 + mixed), index=index, name="blend")


# ----------------------------------------------------------------------------- stress tests


@dataclass(frozen=True)
class RandomEntryResult:
    """The outcome of the random entry test."""

    trades: int
    runs: int
    strategy_mean: float  # average return per trade of the real strategy
    random_mean: float  # average return per trade over all random traders
    random_low: float  # 5th percentile of the random traders' averages
    random_high: float  # 95th percentile
    p_value: float  # share of random traders that did as well or better


def random_entry_test(
    result: BacktestResult,
    bars: Bars,
    symbols: Sequence[str],
    costs: CostModel,
    runs: int,
    seed: int,
) -> RandomEntryResult | None:
    """Could random entries have done as well as the strategy's picks?

    Each random trader makes exactly as many trades as the strategy, with
    exactly the same holding lengths, but buys a random symbol from the same
    list on a random day of the same years, at the open, and pays the same
    costs. We compare the average return per trade.

    The p-value is the share of random traders that matched or beat the
    strategy. Small (below 0.05) means "hard to explain by luck". Large means
    the picks themselves added nothing we can measure.
    """
    trades = [t for t in result.trades if t.bars_held >= 1]
    if not trades:
        return None
    part = bars.select(symbols)
    window = part.open.loc[result.equity.index[0] : result.equity.index[-1]]
    opens = window.to_numpy(dtype="float64")
    valid = np.isfinite(opens) & (opens > 0)
    lengths = np.array([t.bars_held for t in trades], dtype=np.int64)
    if lengths.max() >= len(opens):
        return None
    slip = costs.slippage_fraction
    actual = np.array([t.exit_price / t.entry_price - 1 for t in trades])
    rng = np.random.default_rng(seed)
    means: list[np.ndarray] = []
    for done in range(0, runs, RANDOM_CHUNK):
        batch = min(RANDOM_CHUNK, runs - done)
        length = np.tile(lengths, batch)
        entry, column = _random_entries(rng, valid, length)
        gross = opens[entry + length, column] / opens[entry, column]
        returns = gross * (1 - slip) / (1 + slip) - 1
        means.append(returns.reshape(batch, len(lengths)).mean(axis=1))
    random_means = np.concatenate(means)
    strategy_mean = float(actual.mean())
    beaten = int(np.sum(random_means >= strategy_mean))
    return RandomEntryResult(
        trades=len(trades),
        runs=runs,
        strategy_mean=strategy_mean,
        random_mean=float(random_means.mean()),
        random_low=float(np.percentile(random_means, LOW_PERCENTILE)),
        random_high=float(np.percentile(random_means, HIGH_PERCENTILE)),
        p_value=(beaten + 1) / (runs + 1),
    )


def _random_entries(
    rng: np.random.Generator, valid: np.ndarray, length: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Random (day, symbol) pairs where the symbol trades on both the entry and exit day."""
    n_days, n_symbols = valid.shape
    entry = np.zeros(len(length), dtype=np.int64)
    column = np.zeros(len(length), dtype=np.int64)
    todo = np.arange(len(length))
    for _ in range(MAX_RESAMPLE_ROUNDS):
        if todo.size == 0:
            return entry, column
        entry[todo] = (rng.random(todo.size) * (n_days - length[todo])).astype(np.int64)
        column[todo] = rng.integers(0, n_symbols, size=todo.size)
        ok = valid[entry[todo], column[todo]] & valid[entry[todo] + length[todo], column[todo]]
        todo = todo[~ok]
    raise RuntimeError("Could not find enough valid random trades. Is the price data mostly empty?")


# ----------------------------------------------------------------------------- shapes


@dataclass(frozen=True)
class Drawdown:
    """One fall from a high point, and when (if ever) it was made back."""

    peak: pd.Timestamp
    trough: pd.Timestamp
    recovered: pd.Timestamp | None
    depth: float


def worst_drawdowns(curve: pd.Series, base: float, count: int = 3) -> list[Drawdown]:
    """The ``count`` deepest separate drawdowns, worst first."""
    values = curve.to_numpy(dtype="float64")
    dates = list(curve.index)
    found: list[Drawdown] = []
    peak_value, peak_date = base, pd.Timestamp(dates[0])
    trough_value, trough_date = base, pd.Timestamp(dates[0])
    for value, date in zip(values, dates, strict=True):
        if value >= peak_value:
            if trough_value < peak_value:
                found.append(
                    Drawdown(
                        peak_date, trough_date, pd.Timestamp(date), trough_value / peak_value - 1
                    )
                )
            peak_value, peak_date = value, pd.Timestamp(date)
            trough_value, trough_date = value, pd.Timestamp(date)
        elif value < trough_value:
            trough_value, trough_date = value, pd.Timestamp(date)
    if trough_value < peak_value:
        found.append(Drawdown(peak_date, trough_date, None, trough_value / peak_value - 1))
    return sorted(found, key=lambda d: d.depth)[:count]


def drawdown_curve(curve: pd.Series, base: float) -> pd.Series:
    """How far below its best level so far the account is, every day (0 = at a high)."""
    peaks = np.maximum.accumulate(np.maximum(curve.to_numpy(dtype="float64"), base))
    return pd.Series(curve.to_numpy() / peaks - 1, index=curve.index, name="drawdown")


def period_returns(curve: pd.Series, base: float, freq: str) -> pd.Series:
    """Returns per calendar period: ``freq`` is "ME" for months or "YE" for years."""
    ends = curve.resample(freq).last().dropna()
    previous = ends.shift(1)
    previous.iloc[0] = base
    return ends / previous - 1


def monthly_table(curve: pd.Series, base: float) -> pd.DataFrame:
    """Monthly returns as a table: one row per year, one column per month."""
    monthly = period_returns(curve, base, "ME")
    when = pd.DatetimeIndex(monthly.index)
    table = pd.DataFrame({"year": when.year, "month": when.month, "ret": monthly.to_numpy()})
    return table.pivot(index="year", columns="month", values="ret")
