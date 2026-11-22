"""Position sizing and the daily plan: the shared code path of backtest and bot."""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd
import pytest

from hsw_bot.config import SizingRules
from hsw_bot.data import Bars
from hsw_bot.planner import Book, plan_day, size_buy
from hsw_bot.strategies.base import Day, Entry, Exit, Position, Strategy
from tests import synthetic


def rules(**changes: float) -> SizingRules:
    values: dict[str, float] = {
        "max_positions": 3,
        "max_position_pct": 0.25,
        "risk_per_trade": 0.01,
        "cash_reserve_pct": 0.0,
        "min_order_dollars": 10.0,
    }
    values.update(changes)
    return SizingRules(
        max_positions=int(values["max_positions"]),
        max_position_pct=values["max_position_pct"],
        risk_per_trade=values["risk_per_trade"],
        cash_reserve_pct=values["cash_reserve_pct"],
        min_order_dollars=values["min_order_dollars"],
    )


def test_risk_sizing_loses_one_percent_at_the_stop() -> None:
    # $10,000 account, 1% risk = $100. Stop $5 below a $100 price -> 20 shares -> $2,000.
    entry = Entry("AAA", 1.0, "test", stop_distance=5.0)
    assert size_buy(entry, 100.0, 10_000.0, 10_000.0, rules()) == 2000.0


def test_position_cap_wins_when_the_stop_is_tight() -> None:
    # Stop only $1 away would mean $10,000; the 25% cap keeps it at $2,500.
    entry = Entry("AAA", 1.0, "test", stop_distance=1.0)
    assert size_buy(entry, 100.0, 10_000.0, 10_000.0, rules()) == 2500.0


def test_weight_and_cash_limits() -> None:
    entry = Entry("AAA", 1.0, "test", weight=1 / 3)
    assert size_buy(entry, 50.0, 9_000.0, 9_000.0, rules(max_position_pct=1.0)) == 3000.0
    assert size_buy(entry, 50.0, 9_000.0, 1_634.567, rules(max_position_pct=1.0)) == 1634.56


def test_a_buy_waits_for_cash_rather_than_starting_tiny() -> None:
    # Planned: one third of $9,000 = $3,000. Cash for less than half of it: wait.
    entry = Entry("AAA", 1.0, "test", weight=1 / 3)
    assert size_buy(entry, 50.0, 9_000.0, 1_499.0, rules(max_position_pct=1.0)) == 0.0
    assert size_buy(entry, 50.0, 9_000.0, 1_500.0, rules(max_position_pct=1.0)) == 1500.0


def test_bad_inputs_size_to_zero() -> None:
    entry = Entry("AAA", 1.0, "test", stop_distance=float("nan"))
    assert size_buy(entry, 100.0, 10_000.0, 10_000.0, rules()) == 0.0
    assert (
        size_buy(Entry("AAA", 1.0, "x", weight=0.5), float("nan"), 10_000.0, 10_000.0, rules())
        == 0.0
    )


class Wishes(Strategy):
    """Wants to buy every symbol it is told about and sell everything it holds."""

    name = "wishes"
    title = "Test wishes"
    Params = dict

    def warmup_days(self) -> int:
        return 0

    def indicators(self, bars: Bars) -> dict[str, pd.DataFrame]:
        return {}

    def exits(self, day: Day, positions: Mapping[str, Position]) -> list[Exit]:
        return [Exit(s, "sell") for s in positions]

    def entries(self, day: Day, positions: Mapping[str, Position]) -> list[Entry]:
        wanted = ["S01", "S02", "S03", "S04", "OUTSIDER"]
        return [Entry(s, 10.0 - i, "buy", weight=0.2) for i, s in enumerate(wanted)]


def make_day(bars: Bars) -> Day:
    return Day(400, bars.dates, {"close": bars.close})


def test_plan_respects_slots_blocks_and_the_strategy_list(bars: Bars) -> None:
    strategy = Wishes({}, ("S01", "S02", "S03", "S04"))
    book = Book(cash=10_000.0, equity=10_000.0, positions={})
    plan = plan_day(
        strategy,
        make_day(bars),
        book,
        rules(max_position_pct=1.0),
        synthetic.costs(),
        blocked={"S02"},
    )
    assert [o.symbol for o in plan.buys] == ["S01", "S03", "S04"]
    assert all(o.notional == 2000.0 for o in plan.buys)
    skipped = dict(plan.skipped)
    assert skipped["S02"] == "blocked today"
    assert "list" in skipped["OUTSIDER"]


def test_no_entries_means_sells_only(bars: Bars) -> None:
    strategy = Wishes({}, ("S01", "S02"))
    day = make_day(bars)
    held = Position("S01", 5.0, 100.0, bars.dates[390], bars.dates[389])
    book = Book(cash=5_000.0, equity=5_500.0, positions={"S01": held})
    plan = plan_day(strategy, day, book, rules(), synthetic.costs(), allow_entries=False)
    assert [(o.side, o.symbol, o.qty) for o in plan.orders] == [("sell", "S01", 5.0)]


def test_a_stock_sold_today_is_not_bought_back_today(bars: Bars) -> None:
    strategy = Wishes({}, ("S01", "S02"))
    held = Position("S01", 5.0, 100.0, bars.dates[390], bars.dates[389])
    book = Book(cash=5_000.0, equity=5_500.0, positions={"S01": held})
    plan = plan_day(strategy, make_day(bars), book, rules(), synthetic.costs())
    assert "S01" not in {o.symbol for o in plan.buys}


@pytest.mark.parametrize("reserve", [0.0, 0.05])
def test_buys_never_spend_more_than_the_cash(bars: Bars, reserve: float) -> None:
    strategy = Wishes({}, ("S01", "S02", "S03", "S04"))
    book = Book(cash=3_000.0, equity=10_000.0, positions={})
    plan = plan_day(
        strategy,
        make_day(bars),
        book,
        rules(max_positions=4, max_position_pct=1.0, cash_reserve_pct=reserve),
        synthetic.costs(),
    )
    assert sum(o.notional or 0 for o in plan.buys) <= 3_000.0 - reserve * 10_000.0 + 1e-9


def test_plan_day_never_counts_money_from_todays_sells(bars: Bars) -> None:
    """A sell fills at the next open and its cash settles later: tonight's buys cannot use it."""
    strategy = Wishes({}, ("S01", "S02", "S03"))
    held = Position("S01", 50.0, 100.0, bars.dates[390], bars.dates[389])
    book = Book(cash=300.0, equity=5_300.0, positions={"S01": held})
    plan = plan_day(strategy, make_day(bars), book, rules(max_position_pct=1.0), synthetic.costs())
    assert [o.symbol for o in plan.sells] == ["S01"]
    assert sum(o.notional or 0 for o in plan.buys) <= book.cash


def test_fit_to_cash_drops_what_does_not_fit() -> None:
    from hsw_bot.planner import OrderPlan, fit_to_cash

    wishes = [
        ("a", OrderPlan("X", "buy", "r", notional=600.0)),
        ("a", OrderPlan("Y", "buy", "r", notional=500.0)),
        ("b", OrderPlan("Z", "buy", "r", notional=300.0)),
    ]
    kept, dropped = fit_to_cash(wishes, 1_000.0)
    assert [o.symbol for _, o in kept] == ["X", "Z"]
    assert [o.symbol for _, o in dropped] == ["Y"]


def test_frozen_positions_keep_their_slot_and_get_no_exit(bars: Bars) -> None:
    """A position with a bad price bar today still fills its slot and uses money,
    as in the backtest, but no exit is judged on a price we cannot trust."""
    strategy = Wishes({}, ("S01", "S02", "S03", "S04"))
    held = Position("S01", 5.0, 100.0, bars.dates[390], bars.dates[389])
    book = Book(cash=5_000.0, equity=5_500.0, positions={"S01": held})
    plan = plan_day(
        strategy,
        make_day(bars),
        book,
        rules(max_positions=2, max_position_pct=1.0),
        synthetic.costs(),
        frozen={"S01"},
    )
    assert not plan.sells, "the frozen position is not judged"
    assert [o.symbol for o in plan.buys] == ["S02"], "and it still takes one of the two slots"
