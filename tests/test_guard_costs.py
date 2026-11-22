"""Guard 3: higher costs must always mean a lower result.

If paying more per trade ever made a backtest look better, the cost model
would be broken (for example, charging the wrong side). We run every strategy
at four cost levels and demand a strictly falling final balance.
"""

from __future__ import annotations

import itertools

import pytest

from hsw_bot.config import CostModel
from hsw_bot.data import Bars
from hsw_bot.engine import run_backtest
from tests import synthetic

COST_LEVELS_BPS = (0.0, 5.0, 10.0, 20.0)


def final_equity(name: str, bars: Bars, costs: CostModel) -> float:
    result = run_backtest(
        synthetic.STRATEGY_BUILDERS[name](),
        bars,
        costs,
        synthetic.STRATEGY_RULES[name],
        10_000.0,
        start=bars.dates[150],
    )
    assert result.trades
    return float(result.equity.iloc[-1])


@pytest.mark.parametrize("name", sorted(synthetic.STRATEGY_BUILDERS))
def test_more_slippage_means_less_money(name: str, bars: Bars) -> None:
    results = [final_equity(name, bars, synthetic.costs(bps)) for bps in COST_LEVELS_BPS]
    assert all(a > b for a, b in itertools.pairwise(results)), results


@pytest.mark.parametrize("name", sorted(synthetic.STRATEGY_BUILDERS))
def test_commission_means_less_money(name: str, bars: Bars) -> None:
    free = final_equity(name, bars, synthetic.costs(5.0, commission=0.0))
    paid = final_equity(name, bars, synthetic.costs(5.0, commission=1.0))
    assert paid < free


def test_stress_multiplier_doubles_every_cost() -> None:
    base = CostModel(slippage_bps=5.0, commission_per_order=0.5, stress_multiplier=2.0)
    stressed = base.scaled(base.stress_multiplier)
    assert stressed.slippage_bps == 10.0
    assert stressed.commission_per_order == 1.0
