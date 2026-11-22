"""Guard 1: the golden baseline.

A tiny market (one stock, six days, round numbers) and a scripted strategy
whose result we worked out by hand. If the engine's arithmetic ever changes,
this test fails, and you know before any real money depends on it.

The hand calculation (10 bps per side, $1 commission per order, $1,000 cash):

    day 1 close : the script asks to put half the account (weight 0.5) in AAA
                  -> a buy order for $500.00 at the next open
    day 2 open  : 102 x 1.001 = 102.102 a share  -> 500 / 102.102 = 4.8970637 shares
                  cash = 1000 - 500 - 1 (commission) = 499
    day 2 close : equity = 499 + 4.8970637 x 104 = 1008.2946
    day 3 close : equity = 499 + 4.8970637 x 110 = 1037.6770; the script asks to sell
    day 4 open  : 110 x 0.999 = 109.89 a share -> proceeds 538.1383
                  cash = 499 + 538.1383 - 1 = 1036.1383
    trade P&L   : 538.1383 - 1 - (500 + 1) = 36.1383, which is 7.2132% of the $501 paid
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

from hsw_bot.config import CostModel, SizingRules
from hsw_bot.data import Bars
from hsw_bot.engine import run_backtest
from hsw_bot.strategies.base import Day, Entry, Exit, Position, Strategy
from tests import synthetic

GOLDEN_FILE = Path(__file__).parent / "golden" / "snapshot.json"


@dataclass(frozen=True)
class ScriptParams:
    buy_on: int
    sell_on: int


class Script(Strategy):
    """Buys on one fixed evening and sells on another. Only for this test."""

    name = "script"
    title = "Scripted test strategy"
    Params = ScriptParams
    params: ScriptParams

    def warmup_days(self) -> int:
        return 0

    def indicators(self, bars: Bars) -> dict[str, pd.DataFrame]:
        return {}

    def exits(self, day: Day, positions: Mapping[str, Position]) -> list[Exit]:
        if day.index == self.params.sell_on:
            return [Exit(symbol, "script says sell") for symbol in positions]
        return []

    def entries(self, day: Day, positions: Mapping[str, Position]) -> list[Entry]:
        if day.index == self.params.buy_on:
            return [Entry("AAA", 1.0, "script says buy", weight=0.5)]
        return []


def run_tiny() -> tuple[float, list[float], float, float, int]:
    costs = CostModel(slippage_bps=10.0, commission_per_order=1.0)
    rules = SizingRules(
        max_positions=1,
        max_position_pct=1.0,
        risk_per_trade=0.0,
        cash_reserve_pct=0.0,
        min_order_dollars=1.0,
    )
    result = run_backtest(
        Script(ScriptParams(1, 3), ("AAA",)), synthetic.tiny_bars(), costs, rules, 1000.0
    )
    trade = result.trades[0]
    return result.equity.iloc[-1], list(result.equity), trade.pnl, trade.return_pct, trade.bars_held


def test_tiny_market_matches_the_hand_calculation() -> None:
    final, curve, pnl, ret, held = run_tiny()
    qty = 500 / (102 * 1.001)
    assert qty == pytest.approx(4.897063720593133, rel=1e-12)
    assert curve[0] == pytest.approx(1000.0)
    assert curve[1] == pytest.approx(1000.0)
    assert curve[2] == pytest.approx(499 + qty * 104, rel=1e-12)
    assert curve[2] == pytest.approx(1008.2946269416858, rel=1e-12)
    assert curve[3] == pytest.approx(1037.6770092652446, rel=1e-12)
    assert final == pytest.approx(1036.1383322559793, rel=1e-12)
    assert curve[5] == pytest.approx(final, rel=1e-12)
    assert pnl == pytest.approx(36.13833225597932, rel=1e-12)
    assert ret == pytest.approx(0.07213239971253357, rel=1e-12)
    assert held == 2  # bought on day 2, sold on day 4


def snapshot_values() -> dict[str, dict[str, float]]:
    """Key results of every real strategy on the fake market."""
    bars = synthetic.make_bars()
    values: dict[str, dict[str, float]] = {}
    for name, build in synthetic.STRATEGY_BUILDERS.items():
        result = run_backtest(
            build(),
            bars,
            synthetic.costs(),
            synthetic.STRATEGY_RULES[name],
            10_000.0,
            start=bars.dates[150],
        )
        values[name] = {
            "final_equity": float(result.equity.iloc[-1]),
            "trades": float(len(result.trades)),
            "first_trade_pnl": float(result.trades[0].pnl) if result.trades else 0.0,
        }
    return values


def test_strategies_match_the_saved_snapshot() -> None:
    """A regression snapshot: any change in results must be on purpose.

    If you change a strategy on purpose, refresh the snapshot with:
        python -m tests.make_golden
    """
    saved = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    now = snapshot_values()
    assert set(now) == set(saved)
    for name, values in now.items():
        assert values["trades"] == saved[name]["trades"], name
        assert values["final_equity"] == pytest.approx(saved[name]["final_equity"], rel=1e-9), name
        assert values["first_trade_pnl"] == pytest.approx(saved[name]["first_trade_pnl"], rel=1e-9)
