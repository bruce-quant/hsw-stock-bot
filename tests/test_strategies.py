"""What each strategy does, checked against its own written rules."""

from __future__ import annotations

import numpy as np
import pandas as pd

from hsw_bot.data import Bars, to_float
from hsw_bot.engine import run_backtest
from hsw_bot.planner import build_table
from hsw_bot.strategies.base import Day, Position
from tests import synthetic


def test_steady_decides_on_the_first_trading_day_of_each_month(bars: Bars) -> None:
    steady = synthetic.steady()
    picks = build_table(steady, bars)["pick"]
    changes = picks.ne(picks.shift(1)).any(axis=1)
    changes.iloc[0] = False
    months = pd.DatetimeIndex(picks.index).to_period("M")
    first_days = months != np.roll(months, 1)
    assert changes[changes].index.isin(picks.index[first_days]).all()


def test_steady_holds_top_funds_or_only_the_safe_fund(bars: Bars) -> None:
    steady = synthetic.steady()
    picks = build_table(steady, bars)["pick"]
    counts = picks.sum(axis=1)
    safe_days = picks[synthetic.SAFE]
    assert set(counts[counts > 0].unique()) <= {1, steady.params.top_n}
    assert (counts[safe_days] == 1).all(), "risk off means the safe fund alone"
    assert safe_days.any() and (~safe_days & (counts > 0)).any(), "the fake market has both moods"


def test_steady_risk_off_follows_the_trend_filter(bars: Bars) -> None:
    steady = synthetic.steady()
    table = build_table(steady, bars)
    score = table["momentum"]
    months = pd.DatetimeIndex(score.index).month
    first = np.concatenate([[False], months[1:] != months[:-1]])
    for day in score.index[first]:
        market = to_float(score.at[day, synthetic.MARKET])
        safe = to_float(score.at[day, synthetic.SAFE])
        if np.isnan(market) or np.isnan(safe):
            continue
        assert bool(table["pick"].at[day, synthetic.SAFE]) == bool(market <= safe)


def test_swing_trades_follow_the_written_rules(bars: Bars) -> None:
    swing = synthetic.swing()
    table = build_table(swing, bars)
    result = run_backtest(
        swing, bars, synthetic.costs(), synthetic.STRATEGY_RULES["swing"], 10_000.0
    )
    assert result.trades
    p = swing.params
    dates = list(bars.dates)
    for trade in result.trades:
        s, signal = trade.symbol, trade.signal_date
        assert bool(table["setup"].at[signal, s])
        close = to_float(table["close"].at[signal, s])
        assert close > float(bars.close[s].loc[:signal].iloc[-p.trend_slow_days :].mean())
        decided = dates[dates.index(trade.exit_date) - 1]
        exit_close = to_float(table["close"].at[decided, s])
        reason = trade.exit_reason.split(":")[0]
        if reason == "stop":
            assert exit_close < to_float(table["stop_level"].at[signal, s])
        elif reason == "rebound":
            assert exit_close > to_float(table["band_middle"].at[decided, s])
        else:
            assert reason == "time"
            assert dates.index(decided) - dates.index(trade.entry_date) + 1 >= p.max_hold_days


def test_swing_ranks_more_dips_than_slots_by_score(bars: Bars) -> None:
    swing = synthetic.swing()
    table = build_table(swing, bars)
    row = 400
    setup = table["setup"].copy()
    setup.iloc[row, :] = False
    setup.iloc[row, :4] = True  # pretend four stocks dipped on the same evening
    entries = swing.entries(Day(row, bars.dates, {**table, "setup": setup}), {})
    assert len(entries) == 4
    assert [e.score for e in entries] == sorted((e.score for e in entries), reverse=True)
    assert all(e.stop_distance is not None and e.stop_distance > 0 for e in entries)
    held = {entries[0].symbol: synthetic_position(entries[0].symbol, bars)}
    again = swing.entries(Day(row, bars.dates, {**table, "setup": setup}), held)
    assert entries[0].symbol not in {e.symbol for e in again}, "never buy what we already hold"


def synthetic_position(symbol: str, bars: Bars) -> Position:
    return Position(symbol, 1.0, 100.0, bars.dates[390], bars.dates[389])


def test_every_registered_strategy_is_guarded() -> None:
    """A new strategy in the registry must also get the five guard tests."""
    from hsw_bot.strategies import STRATEGIES

    missing = sorted(set(STRATEGIES) - set(synthetic.STRATEGY_BUILDERS))
    assert not missing, (
        f"Add {missing} to STRATEGY_BUILDERS and STRATEGY_RULES in tests/synthetic.py, "
        "so the guard tests check your strategy too."
    )
