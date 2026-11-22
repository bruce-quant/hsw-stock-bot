"""The backtest: replay history one day at a time, exactly the way the bot trades.

The timing rule is simple and strict:

* in the evening of day t, after the close, the strategies decide;
* the orders fill at the OPEN of day t+1, plus costs.

So a decision can never use a price it could not have known. Each morning we
first fill yesterday's orders, then we value the account at the close, then we
ask the strategies for tomorrow's orders.

Costs: every buy fills a little ABOVE the open and every sell a little BELOW
it (``slippage_bps`` per side), plus any commission per order.

One account can run several strategies ("sleeves"), each with its share of
the money, exactly like the bot. A plain backtest of one strategy is simply
one sleeve with the whole account.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from hsw_bot.config import CostModel, SizingRules
from hsw_bot.data import Bars
from hsw_bot.planner import OrderPlan, Side, SleeveDay, build_table, plan_sleeves
from hsw_bot.strategies.base import Day, Position, Strategy


@dataclass(frozen=True)
class Sleeve:
    """One strategy and its share of the account."""

    name: str
    strategy: Strategy
    rules: SizingRules
    share: float = 1.0


@dataclass(frozen=True)
class Fill:
    """One executed order. ``price`` already includes slippage."""

    date: pd.Timestamp
    symbol: str
    side: Side
    qty: float
    price: float
    commission: float
    reason: str


@dataclass(frozen=True)
class Trade:
    """One finished round trip: bought, then sold."""

    symbol: str
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    qty: float
    entry_price: float
    exit_price: float
    pnl: float
    return_pct: float
    bars_held: int
    entry_reason: str
    exit_reason: str
    sleeve: str = ""


@dataclass
class BacktestResult:
    """Everything a backtest produced."""

    strategy: str
    costs: CostModel
    starting_cash: float
    equity: pd.Series
    cash: pd.Series
    invested: pd.Series
    trades: list[Trade] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    open_positions: dict[str, Position] = field(default_factory=dict)
    final_prices: dict[str, float] = field(default_factory=dict)

    @property
    def unrealized_pnl(self) -> float:
        """Profit or loss on positions still open at the end, valued at the last close."""
        return math.fsum(
            pos.qty * self.final_prices[sym] - pos.cost_basis
            for sym, pos in self.open_positions.items()
        )

    def trades_frame(self) -> pd.DataFrame:
        """The trades as a table, one row per round trip."""
        columns = list(Trade.__dataclass_fields__)
        return pd.DataFrame([t.__dict__ for t in self.trades], columns=columns)


@dataclass
class _Account:
    """The engine's private scratch pad while it walks through history."""

    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    owner: dict[str, str] = field(default_factory=dict)  # symbol -> sleeve
    entry_reasons: dict[str, str] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    carried_sells: list[tuple[str, OrderPlan]] = field(default_factory=list)


def run_backtest(
    strategy: Strategy,
    bars: Bars,
    costs: CostModel,
    rules: SizingRules,
    starting_cash: float,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
) -> BacktestResult:
    """Replay one strategy with the whole account. See ``run_sleeves``."""
    sleeve = Sleeve(strategy.name, strategy, rules, 1.0)
    return run_sleeves([sleeve], bars, costs, starting_cash, start, end)


def run_sleeves(
    sleeves: Sequence[Sleeve],
    bars: Bars,
    costs: CostModel,
    starting_cash: float,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
) -> BacktestResult:
    """Replay one or more strategies sharing one account, and return the equity curve.

    Indicators are computed on ALL bars, including the warm up years before
    ``start``, so a 200 day average is ready on the first trading day.
    """
    symbols = list(dict.fromkeys(s for sleeve in sleeves for s in sleeve.strategy.symbols()))
    bars = bars.select(symbols)
    tables = {sleeve.name: build_table(sleeve.strategy, bars) for sleeve in sleeves}
    dates = bars.dates
    first = 0 if start is None else int(dates.searchsorted(pd.Timestamp(start)))
    last = (
        len(dates) - 1 if end is None else int(dates.searchsorted(pd.Timestamp(end), "right")) - 1
    )
    if first > last:
        raise ValueError("There are no trading days between the start and end dates.")

    opens = bars.open.to_numpy(dtype="float64")
    marks = bars.close.ffill().to_numpy(dtype="float64")
    column = {symbol: j for j, symbol in enumerate(bars.symbols)}
    account = _Account(cash=starting_cash)
    equity_curve: list[float] = []
    cash_curve: list[float] = []
    invested_curve: list[float] = []
    orders: list[tuple[str, OrderPlan]] = []

    for i in range(first, last + 1):
        if orders:
            _fill_orders(account, orders, i, dates, opens, column, costs, sleeves)
        invested = math.fsum(
            pos.qty * marks[i, column[sym]] for sym, pos in account.positions.items()
        )
        equity = account.cash + invested
        equity_curve.append(equity)
        cash_curve.append(account.cash)
        invested_curve.append(invested)
        if i == last:
            break
        prices = {sym: float(marks[i, column[sym]]) for sym in account.positions}
        sleeve_days = [
            SleeveDay(
                name=sleeve.name,
                strategy=sleeve.strategy,
                rules=sleeve.rules,
                share=sleeve.share,
                day=Day(i, dates, tables[sleeve.name]),
                positions={
                    sym: pos
                    for sym, pos in account.positions.items()
                    if account.owner[sym] == sleeve.name
                },
            )
            for sleeve in sleeves
        ]
        plan = plan_sleeves(sleeve_days, prices, costs, account_cash=account.cash, equity=equity)
        orders = plan.orders

    index = dates[first : last + 1]
    final_prices = {sym: float(marks[last, column[sym]]) for sym in account.positions}
    name = sleeves[0].name if len(sleeves) == 1 else "+".join(s.name for s in sleeves)
    return BacktestResult(
        strategy=name,
        costs=costs,
        starting_cash=starting_cash,
        equity=pd.Series(equity_curve, index=index, name="equity"),
        cash=pd.Series(cash_curve, index=index, name="cash"),
        invested=pd.Series(invested_curve, index=index, name="invested"),
        trades=account.trades,
        fills=account.fills,
        open_positions=dict(account.positions),
        final_prices=final_prices,
    )


def _fill_orders(
    account: _Account,
    orders: list[tuple[str, OrderPlan]],
    i: int,
    dates: pd.DatetimeIndex,
    opens: np.ndarray,
    column: dict[str, int],
    costs: CostModel,
    sleeves: Sequence[Sleeve],
) -> None:
    """Fill yesterday's orders at today's open: sells first, then buys.

    If a stock has no open price today (a trading halt, missing data), a sell
    waits for the next day with a price, and a buy is simply dropped.
    """
    date = pd.Timestamp(dates[i])
    slip = costs.slippage_fraction
    fee = costs.commission_per_order
    minimum = {s.name: s.rules.min_order_dollars for s in sleeves}
    sells = [o for o in orders if o[1].side == "sell"]
    sells, account.carried_sells = [*account.carried_sells, *sells], []
    for name, order in sells:
        position = account.positions.get(order.symbol)
        if position is None:
            continue  # already sold (a carried sell and a new sell for the same stock)
        open_price = opens[i, column[order.symbol]]
        if not math.isfinite(open_price):
            account.carried_sells.append((name, order))
            continue
        price = open_price * (1 - slip)
        account.cash += position.qty * price - fee
        pnl = position.qty * price - fee - position.cost_basis
        entry_index = int(dates.get_indexer(pd.DatetimeIndex([position.entry_date]))[0])
        account.trades.append(
            Trade(
                symbol=order.symbol,
                signal_date=position.signal_date,
                entry_date=position.entry_date,
                exit_date=date,
                qty=position.qty,
                entry_price=position.entry_price,
                exit_price=price,
                pnl=pnl,
                return_pct=pnl / position.cost_basis,
                bars_held=i - entry_index,
                entry_reason=account.entry_reasons.pop(order.symbol, ""),
                exit_reason=order.reason,
                sleeve=account.owner.pop(order.symbol, name),
            )
        )
        account.fills.append(
            Fill(date, order.symbol, "sell", position.qty, price, fee, order.reason)
        )
        del account.positions[order.symbol]

    for name, order in (o for o in orders if o[1].side == "buy"):
        open_price = opens[i, column[order.symbol]]
        if order.symbol in account.positions or not math.isfinite(open_price):
            continue
        price = open_price * (1 + slip)
        dollars = min(order.notional or 0.0, account.cash - fee)
        if dollars < minimum[name]:
            continue
        qty = dollars / price
        account.cash -= qty * price + fee
        signal = order.signal_date if order.signal_date is not None else date
        account.positions[order.symbol] = Position(
            symbol=order.symbol,
            qty=qty,
            entry_price=price,
            entry_date=date,
            signal_date=signal,
            entry_commission=fee,
        )
        account.owner[order.symbol] = name
        account.entry_reasons[order.symbol] = order.reason
        account.fills.append(Fill(date, order.symbol, "buy", qty, price, fee, order.reason))
