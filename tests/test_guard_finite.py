"""Guard 2: no NaN, no infinity, anywhere in the output.

A single "not a number" can silently poison an equity curve or an order size.
We run every strategy on a market with holes in it (a missing week, a stock
that lists late, one that stops trading) and check that every output number
is a real, finite number.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from hsw_bot import stats
from hsw_bot.data import Bars
from hsw_bot.engine import run_backtest
from tests import synthetic


@pytest.mark.parametrize("name", sorted(synthetic.STRATEGY_BUILDERS))
def test_every_output_is_finite(name: str, gappy_bars: Bars) -> None:
    strategy = synthetic.STRATEGY_BUILDERS[name]()
    rules = synthetic.STRATEGY_RULES[name]
    result = run_backtest(
        strategy, gappy_bars, synthetic.costs(), rules, 10_000.0, start=gappy_bars.dates[150]
    )

    for curve in (result.equity, result.cash, result.invested):
        assert np.isfinite(curve.to_numpy()).all()
    assert (result.equity > 0).all()
    assert result.trades, "the fake market should produce trades"
    for trade in result.trades:
        numbers = (trade.qty, trade.entry_price, trade.exit_price, trade.pnl, trade.return_pct)
        assert all(math.isfinite(x) for x in numbers)
        assert trade.qty > 0 and trade.entry_price > 0 and trade.exit_price > 0
    for fill in result.fills:
        assert math.isfinite(fill.qty) and math.isfinite(fill.price) and fill.qty > 0

    scores = stats.result_metrics(result)
    for value in (scores.total_return, scores.max_drawdown, scores.sharpe, scores.volatility):
        assert math.isfinite(value)
    assert scores.cagr is not None and math.isfinite(scores.cagr)

    tradable = strategy.tradable()
    test = stats.random_entry_test(
        result, gappy_bars, tradable, synthetic.costs(), runs=200, seed=1
    )
    assert test is not None
    assert all(math.isfinite(x) for x in (test.strategy_mean, test.random_mean, test.p_value))
    assert 0 < test.p_value <= 1


def test_baselines_are_finite(gappy_bars: Bars) -> None:
    start = gappy_bars.dates[150]
    spy = stats.buy_and_hold(gappy_bars, synthetic.MARKET, synthetic.costs(), 10_000.0, start)
    same = stats.equal_weight(gappy_bars, synthetic.STOCKS, synthetic.costs(), 10_000.0, start)
    for curve in (spy, same):
        assert np.isfinite(curve.to_numpy()).all()
        assert (curve > 0).all()
