"""Turn the strategies' wishes into sized orders, for the whole account.

This is the heart of the "one engine" rule. The backtest calls
``plan_sleeves`` once for every day in history. The live bot calls the very
same function once per run. Same strategy code, same sleeve sizing, same cash
rules, same order list. If the backtest and the bot ever disagreed, we could
not trust the backtest at all.

Orders are always for the NEXT open:

* sells are for the whole position (a quantity of shares);
* buys are a dollar amount ("notional"), because Alpaca lets us buy fractional
  shares with a dollar amount, and a dollar amount cannot overdraw the account
  the way a share count can when the price gaps up overnight.

Never borrow: buys only spend cash the account already has. Money from a sell
is NOT counted for buys on the same evening, because that sell only fills at
the next open (and the cash settles a day later). It gets spent on the next
run. So a rotation buys its new fund one day after selling the old one.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from hsw_bot.config import CostModel, SizingRules
from hsw_bot.data import Bars
from hsw_bot.strategies.base import Day, Entry, Exit, Position, Strategy

CENTS = 100.0
Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class OrderPlan:
    """One order we intend to send for the next open."""

    symbol: str
    side: Side
    reason: str
    qty: float | None = None  # sells: shares (the whole position)
    notional: float | None = None  # buys: dollars to spend
    score: float | None = None
    signal_date: pd.Timestamp | None = None


@dataclass(frozen=True)
class Book:
    """One strategy's money and positions as of today's close."""

    cash: float
    equity: float
    positions: Mapping[str, Position]


@dataclass(frozen=True)
class DayPlan:
    """Everything one strategy decided on one evening."""

    sells: list[OrderPlan] = field(default_factory=list)
    buys: list[OrderPlan] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (symbol, why not bought)

    @property
    def orders(self) -> list[OrderPlan]:
        return [*self.sells, *self.buys]


@dataclass(frozen=True)
class SleeveDay:
    """One strategy's slice of the account on one evening.

    A "sleeve" is the part of the account one strategy runs. ``share`` is its
    slice of the money (0.5 = half). A share of 0 means "sell only".
    ``frozen`` positions count for the slice's money and slots, but their
    exits are not judged today (the live bot uses it for a bad price bar).
    """

    name: str
    strategy: Strategy
    rules: SizingRules
    share: float
    day: Day
    positions: Mapping[str, Position]
    frozen: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AccountPlan:
    """Everything decided for the whole account on one evening."""

    orders: list[tuple[str, OrderPlan]]  # (sleeve, order): all sells first, then buys
    skipped: list[tuple[str, str, str]]  # (sleeve, symbol, why it was not bought)


def build_table(strategy: Strategy, bars: Bars) -> dict[str, pd.DataFrame]:
    """The strategy's indicators plus the raw prices, checked to line up with the bars.

    The backtest and the bot both build the table this way, then hand the
    strategy a ``Day`` view of it. The strategy only ever sees its own symbols.
    """
    bars = bars.select(strategy.symbols())
    table: dict[str, pd.DataFrame] = {
        "open": bars.open,
        "high": bars.high,
        "low": bars.low,
        "close": bars.close,
    }
    for name, frame in strategy.indicators(bars).items():
        if not frame.index.equals(bars.close.index):
            raise ValueError(
                f"Strategy {strategy.name!r}: indicator {name!r} has different dates than the "
                "bars. Build every indicator from the bars you were given."
            )
        table[name] = frame
    return table


def sleeve_book(
    share: float, equity: float, positions: Mapping[str, Position], prices: Mapping[str, float]
) -> Book:
    """One strategy's slice: ``share`` of the equity, minus what its positions are worth."""
    value = math.fsum(pos.qty * _price(prices, pos) for pos in positions.values())
    slice_equity = share * equity
    return Book(cash=max(slice_equity - value, 0.0), equity=slice_equity, positions=positions)


def plan_sleeves(
    sleeves: Sequence[SleeveDay],
    prices: Mapping[str, float],
    costs: CostModel,
    *,
    account_cash: float,
    equity: float,
    allow_entries: bool = True,
    blocked: Collection[str] = (),
) -> AccountPlan:
    """Plan every strategy's orders for one evening, then fit the buys to the real cash.

    ``equity`` is the money the bot runs (the whole account in a backtest; the
    account minus anything you hold by hand, live). ``account_cash`` is the
    cash the account really has right now.
    """
    sells: list[tuple[str, OrderPlan]] = []
    buys: list[tuple[str, OrderPlan]] = []
    skipped: list[tuple[str, str, str]] = []
    for sleeve in sleeves:
        book = sleeve_book(sleeve.share, equity, sleeve.positions, prices)
        plan = plan_day(
            sleeve.strategy,
            sleeve.day,
            book,
            sleeve.rules,
            costs,
            allow_entries=allow_entries and sleeve.share > 0,
            blocked=blocked,
            frozen=sleeve.frozen,
        )
        sells += [(sleeve.name, order) for order in plan.sells]
        buys += [(sleeve.name, order) for order in plan.buys]
        skipped += [(sleeve.name, symbol, why) for symbol, why in plan.skipped]
    reserve = max((s.rules.cash_reserve_pct for s in sleeves), default=0.0)
    available = account_cash - reserve * equity
    kept, dropped = fit_to_cash(buys, available, costs.commission_per_order)
    skipped += [(name, order.symbol, "not enough cash in the account") for name, order in dropped]
    return AccountPlan(orders=[*sells, *kept], skipped=skipped)


def fit_to_cash(
    buys: Sequence[tuple[str, OrderPlan]], available: float, fee: float = 0.0
) -> tuple[list[tuple[str, OrderPlan]], list[tuple[str, OrderPlan]]]:
    """Keep buys, best first, while they fit in the cash; drop the rest.

    Each strategy sized its buys from its own slice. This is the last check
    for the account as a whole: the buys together may never spend more cash
    than the account has. Sell proceeds are never counted here.
    """
    kept: list[tuple[str, OrderPlan]] = []
    dropped: list[tuple[str, OrderPlan]] = []
    for name, order in buys:
        cost = (order.notional or 0.0) + fee
        if cost <= available:
            kept.append((name, order))
            available -= cost
        else:
            dropped.append((name, order))
    return kept, dropped


def plan_day(
    strategy: Strategy,
    day: Day,
    book: Book,
    rules: SizingRules,
    costs: CostModel,
    *,
    allow_entries: bool = True,
    blocked: Collection[str] = (),
    frozen: Collection[str] = (),
) -> DayPlan:
    """Ask one strategy what to do today and size the answer.

    ``allow_entries=False`` is how the safety switches (the STOP file, the loss
    limits, bad data) stop new buys while still letting the strategy sell.
    ``blocked`` lists symbols we must not buy today, for example because an
    order for them is already waiting at the broker. ``frozen`` positions
    still fill a slot and use money, but get no exit decision today.
    """
    judged = {s: p for s, p in book.positions.items() if s not in frozen}
    exits = _valid_exits(strategy.exits(day, judged), judged)
    sells = [
        OrderPlan(
            e.symbol, "sell", e.reason, qty=book.positions[e.symbol].qty, signal_date=day.date
        )
        for e in exits
    ]
    if not allow_entries:
        return DayPlan(sells=sells)

    selling = {e.symbol for e in exits}
    # Only cash we already have. Sell proceeds arrive at the next open: spent next run.
    cash = book.cash - book.equity * rules.cash_reserve_pct
    open_slots = rules.max_positions - (len(book.positions) - len(selling))

    buys: list[OrderPlan] = []
    skipped: list[tuple[str, str]] = []
    allowed = set(strategy.tradable())
    for entry in strategy.entries(day, book.positions):
        if entry.symbol in book.positions or entry.symbol in selling:
            continue
        if entry.symbol not in allowed:
            skipped.append((entry.symbol, "not in this strategy's list"))
            continue
        if entry.symbol in blocked:
            skipped.append((entry.symbol, "blocked today"))
            continue
        if open_slots <= 0:
            skipped.append((entry.symbol, "no free slot"))
            continue
        price = day.value("close", entry.symbol)
        dollars = size_buy(entry, price, book.equity, cash - costs.commission_per_order, rules)
        if dollars < rules.min_order_dollars:
            skipped.append((entry.symbol, "too small, or waiting for cash"))
            continue
        buys.append(
            OrderPlan(
                entry.symbol,
                "buy",
                entry.reason,
                notional=dollars,
                score=entry.score,
                signal_date=day.date,
            )
        )
        cash -= dollars + costs.commission_per_order
        open_slots -= 1
    return DayPlan(sells=sells, buys=buys, skipped=skipped)


def size_buy(entry: Entry, price: float, equity: float, cash: float, rules: SizingRules) -> float:
    """How many dollars to put into one new position.

    Every limit that applies gives a dollar amount, and we take the smallest:

    * the position cap: ``max_position_pct`` of the account;
    * the strategy's weight, if it gave one (e.g. one third of the account);
    * the risk cap, if the strategy gave a stop distance: if the price falls
      to the stop, we lose about ``risk_per_trade`` of the account.
      Shares = (equity x risk) / stop distance, so dollars = shares x price;
    * the cash we have left.

    If the cash covers less than ``min_fill_pct`` of the planned size, the
    answer is 0: the buy waits for the cash. The result is rounded DOWN to
    whole cents, because brokers take cents.
    """
    if not (math.isfinite(price) and price > 0 and equity > 0):
        return 0.0
    caps = [equity * rules.max_position_pct]
    if entry.weight is not None:
        caps.append(equity * entry.weight)
    if entry.stop_distance is not None and rules.risk_per_trade > 0:
        if not (math.isfinite(entry.stop_distance) and entry.stop_distance > 0):
            return 0.0
        caps.append(equity * rules.risk_per_trade / entry.stop_distance * price)
    planned = min(caps)
    dollars = min(planned, cash)
    if dollars <= 0 or dollars < planned * rules.min_fill_pct:
        return 0.0  # not enough cash yet: wait for it rather than open a tiny position
    return math.floor(dollars * CENTS) / CENTS


def _price(prices: Mapping[str, float], position: Position) -> float:
    """The newest good price for a position; its entry price if there is none."""
    price = prices.get(position.symbol, math.nan)
    return price if math.isfinite(price) else position.entry_price


def _valid_exits(exits: list[Exit], positions: Mapping[str, Position]) -> list[Exit]:
    """Keep one exit per symbol we actually hold (a strategy bug must not sell air)."""
    seen: set[str] = set()
    valid: list[Exit] = []
    for e in exits:
        if e.symbol in positions and e.symbol not in seen:
            seen.add(e.symbol)
            valid.append(e)
    return valid
