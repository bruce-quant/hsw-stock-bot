"""Guard 4: no peeking at the future.

The most common way a backtest lies is by using tomorrow's prices today.
We check it three ways, for every strategy in the registry:

1. Indicators computed on data that ENDS on day T are identical, up to T,
   to indicators computed on the full data.
2. Scrambling every price after day T changes nothing before T: the same
   trades, the same equity curve.
3. Every fill happens at the open of the day AFTER the decision, plus costs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hsw_bot.data import Bars, to_float
from hsw_bot.engine import run_backtest
from hsw_bot.planner import build_table
from hsw_bot.strategies.base import Day, LookaheadError
from tests import synthetic

CUT = 500  # the day we cut or scramble the data after


def scramble_after(bars: Bars, day: pd.Timestamp, seed: int = 99) -> Bars:
    """The same market, but every price after ``day`` is replaced with nonsense."""
    rng = np.random.default_rng(seed)
    later = bars.dates > day
    factor = pd.DataFrame(
        1.0 + 0.5 * rng.random(bars.close.shape), index=bars.close.index, columns=bars.close.columns
    )
    factor.loc[~later, :] = 1.0
    frames = {name: getattr(bars, name) * factor for name in ("open", "high", "low", "close")}
    high = frames["high"].clip(lower=frames["open"]).clip(lower=frames["close"])
    low = frames["low"].clip(upper=frames["open"]).clip(upper=frames["close"])
    return Bars(frames["open"], high, low, frames["close"], bars.volume)


@pytest.mark.parametrize("name", sorted(synthetic.STRATEGY_BUILDERS))
def test_indicators_do_not_change_when_the_future_is_cut_off(name: str, bars: Bars) -> None:
    strategy = synthetic.STRATEGY_BUILDERS[name]()
    cut_day = bars.dates[CUT]
    full = build_table(strategy, bars)
    short = build_table(strategy, bars.until(cut_day))
    for key, frame in short.items():
        expected = full[key].loc[:cut_day]
        pd.testing.assert_frame_equal(frame, expected, check_freq=False, obj=f"{name}.{key}")


@pytest.mark.parametrize("name", sorted(synthetic.STRATEGY_BUILDERS))
def test_scrambled_future_changes_nothing_before_it(name: str, bars: Bars) -> None:
    cut_day = bars.dates[CUT]
    rules = synthetic.STRATEGY_RULES[name]
    start = bars.dates[150]
    honest = run_backtest(
        synthetic.STRATEGY_BUILDERS[name](), bars, synthetic.costs(), rules, 10_000.0, start
    )
    scrambled = run_backtest(
        synthetic.STRATEGY_BUILDERS[name](),
        scramble_after(bars, cut_day),
        synthetic.costs(),
        rules,
        10_000.0,
        start,
    )
    pd.testing.assert_series_equal(honest.equity.loc[:cut_day], scrambled.equity.loc[:cut_day])
    before = [t for t in honest.trades if t.exit_date <= cut_day]
    assert before, "need trades before the cut to make this test meaningful"
    assert before == [t for t in scrambled.trades if t.exit_date <= cut_day]


@pytest.mark.parametrize("name", sorted(synthetic.STRATEGY_BUILDERS))
def test_fills_happen_at_the_next_open(name: str, bars: Bars) -> None:
    costs = synthetic.costs(7.0)
    result = run_backtest(
        synthetic.STRATEGY_BUILDERS[name](),
        bars,
        costs,
        synthetic.STRATEGY_RULES[name],
        10_000.0,
        start=bars.dates[150],
    )
    slip = costs.slippage_fraction
    dates = list(bars.dates)
    for trade in result.trades:
        assert dates.index(trade.entry_date) == dates.index(trade.signal_date) + 1
        assert trade.entry_price == pytest.approx(
            to_float(bars.open.at[trade.entry_date, trade.symbol]) * (1 + slip)
        )
        assert trade.exit_price == pytest.approx(
            to_float(bars.open.at[trade.exit_date, trade.symbol]) * (1 - slip)
        )


def test_day_refuses_to_look_ahead(bars: Bars) -> None:
    table = {"close": bars.close}
    day = Day(100, bars.dates, table)
    assert day.on("close", bars.dates[99], "S01") == bars.close.iat[99, 0]
    with pytest.raises(LookaheadError):
        day.on("close", bars.dates[101], "S01")


def test_live_bot_history_gives_the_same_indicators(bars: Bars) -> None:
    """The bot downloads only a few hundred days. Its indicators must match the backtest's."""
    for name, build in synthetic.STRATEGY_BUILDERS.items():
        strategy = build()
        need = strategy.warmup_days()
        recent = Bars(
            *(
                getattr(bars, f).iloc[-(need + 60) :]
                for f in ("open", "high", "low", "close", "volume")
            )
        )
        full = build_table(strategy, bars)
        short = build_table(strategy, recent)
        for key in short:
            if key == "pick":
                continue  # the monthly picks depend on the first month seen; checked below
            tail = short[key].iloc[need:]
            pd.testing.assert_frame_equal(
                tail, full[key].loc[tail.index], check_freq=False, obj=f"{name}.{key}"
            )
        if "pick" in short:
            # after the first full month in the short history, the picks agree
            first_month = short["pick"].index[need:][0].to_period("M") + 1
            later = pd.DatetimeIndex(short["pick"].index).to_period("M") > first_month
            pd.testing.assert_frame_equal(
                short["pick"][later], full["pick"].loc[short["pick"].index[later]]
            )
