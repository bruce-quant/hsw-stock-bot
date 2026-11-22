"""Guard 5: the money adds up.

Every dollar must be accounted for:

* final equity - starting cash = the profit of every closed trade
  + the open profit of every position still held;
* the cash at the end = the starting cash plus every sale minus every buy;
* every day, equity = cash + the value of the positions.

If any of these fails, the engine is creating or losing money somewhere.
"""

from __future__ import annotations

import math

import pytest

from hsw_bot.data import Bars
from hsw_bot.engine import run_backtest
from tests import synthetic


@pytest.mark.parametrize("name", sorted(synthetic.STRATEGY_BUILDERS))
@pytest.mark.parametrize("commission", [0.0, 1.0])
def test_trade_profits_add_up_to_the_equity_change(
    name: str, commission: float, bars: Bars
) -> None:
    start_cash = 10_000.0
    result = run_backtest(
        synthetic.STRATEGY_BUILDERS[name](),
        bars,
        synthetic.costs(5.0, commission=commission),
        synthetic.STRATEGY_RULES[name],
        start_cash,
        start=bars.dates[150],
    )
    closed = math.fsum(t.pnl for t in result.trades)
    change = result.equity.iloc[-1] - start_cash
    assert change == pytest.approx(closed + result.unrealized_pnl, abs=1e-6)

    cash = start_cash
    for fill in result.fills:
        if fill.side == "buy":
            cash -= fill.qty * fill.price + fill.commission
        else:
            cash += fill.qty * fill.price - fill.commission
    assert cash == pytest.approx(result.cash.iloc[-1], abs=1e-6)

    rebuilt = result.cash + result.invested
    assert (rebuilt - result.equity).abs().max() < 1e-6
    assert (result.cash > -1e-6).all(), "the account must never go below zero cash"
