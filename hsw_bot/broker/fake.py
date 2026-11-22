"""A pretend broker that lives in memory.

Used by ``hsw-bot dry-run`` (so you can see real orders without keys) and by
the tests (so every safety rule is checked without the internet).

Like a real broker after the close, it does NOT fill orders right away. They
wait as "accepted" until ``open_session`` is called with the next open prices.
That is exactly how a real order sent in the evening behaves, and it lets the
tests check that the bot never mistakes "accepted" for "filled".
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from hsw_bot.broker.base import (
    AccountSnapshot,
    AssetInfo,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    CashMovement,
    DuplicateOrderError,
    MarketClock,
    OrderRequest,
)

WEEKEND = 5  # Saturday is weekday 5 in Python
OPEN_HOUR, OPEN_MINUTE, CLOSE_HOUR = 9, 30, 16


@dataclass
class FakeBroker:
    """An in memory broker. All money moves happen in ``open_session``."""

    cash: float
    now: Callable[[], datetime]
    last_equity: float | None = None
    positions: dict[str, BrokerPosition] = field(default_factory=dict)
    orders: dict[str, BrokerOrder] = field(default_factory=dict)
    fail: set[str] = field(default_factory=set)  # method names that should raise, for tests
    not_fractionable: set[str] = field(default_factory=set)
    reject: set[str] = field(default_factory=set)  # symbols whose orders get rejected, for tests
    blocked: bool = False
    session: date | None = None  # what last_session() reports; None means "unknown"
    balance_asof: date | None = None  # the day last_equity is from; None means "unknown"
    multiplier: float = 1.0
    account_id: str = "fake-account-1"
    account_number: str = "FAKE0001"
    daily_equity: list[tuple[datetime, float]] = field(default_factory=list)  # portfolio history
    cash_movements: list[CashMovement] = field(default_factory=list)  # deposits and withdrawals
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    @property
    def label(self) -> str:
        return "fake broker"

    # ------------------------------------------------------------------ reads
    def get_account(self) -> AccountSnapshot:
        self._maybe_fail("get_account")
        equity = self.cash + math.fsum(p.market_value for p in self.positions.values())
        last = equity if self.last_equity is None else self.last_equity
        return AccountSnapshot(
            account_id=self.account_id,
            account_number=self.account_number,
            equity=equity,
            last_equity=last,
            cash=self.cash,
            buying_power=self.cash * self.multiplier,
            blocked=self.blocked,
            status="ACTIVE",
            multiplier=self.multiplier,
            balance_asof=self.balance_asof,
        )

    def get_positions(self) -> list[BrokerPosition]:
        self._maybe_fail("get_positions")
        return list(self.positions.values())

    def get_open_orders(self) -> list[BrokerOrder]:
        self._maybe_fail("get_open_orders")
        return [o for o in self.orders.values() if o.is_open]

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        self._maybe_fail("get_order")
        return self.orders.get(client_order_id)

    def get_order_history(self, symbols: Sequence[str] | None = None) -> list[BrokerOrder]:
        self._maybe_fail("get_order_history")
        wanted = None if symbols is None else set(symbols)
        # A dict keeps insertion order, and orders are inserted as they are sent.
        return [o for o in self.orders.values() if wanted is None or o.symbol in wanted]

    def get_daily_equity(self, since: date) -> list[tuple[datetime, float]]:
        self._maybe_fail("get_daily_equity")
        return [(label, value) for label, value in self.daily_equity if label.date() >= since]

    def get_cash_movements(self, since: date) -> list[CashMovement]:
        self._maybe_fail("get_cash_movements")
        return [m for m in self.cash_movements if m.day >= since]

    def get_clock(self) -> MarketClock:
        self._maybe_fail("get_clock")
        now = self.now()
        opens = now.replace(hour=OPEN_HOUR, minute=OPEN_MINUTE, second=0, microsecond=0)
        closes = now.replace(hour=CLOSE_HOUR, minute=0, second=0, microsecond=0)
        weekday = now.weekday() < WEEKEND
        is_open = weekday and opens <= now < closes
        next_open = opens if (weekday and now < opens) else _next_weekday(opens)
        next_close = closes if (weekday and now < closes) else _next_weekday(closes)
        return MarketClock(now, is_open, next_open, next_close)

    def last_session(self, now: datetime) -> date | None:
        self._maybe_fail("last_session")
        return self.session

    def get_asset(self, symbol: str) -> AssetInfo:
        self._maybe_fail("get_asset")
        return AssetInfo(symbol, True, symbol not in self.not_fractionable)

    # ------------------------------------------------------------------ writes
    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        self._maybe_fail("submit_order")
        if request.client_order_id in self.orders:
            raise DuplicateOrderError("client_order_id must be unique")
        status = "accepted"
        if request.symbol in self.reject or request.symbol in self.not_fractionable:
            status = "rejected"
        order = BrokerOrder(
            order_id=f"fake-{next(self._ids)}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            status=status,
            qty=request.qty,
            notional=request.notional,
            filled_qty=0.0,
            filled_avg_price=None,
            submitted_at=self.now(),
            filled_at=None,
        )
        self.orders[request.client_order_id] = order
        return order

    def open_session(self, prices: Mapping[str, float], when: datetime) -> None:
        """Pretend the market opened: fill every waiting order at ``prices``.

        Sells fill first, then buys, the same order the backtest uses.
        Then every position is valued at its fill price.
        """
        waiting = [o for o in self.orders.values() if o.is_open]
        for order in sorted(waiting, key=lambda o: o.side != "sell"):
            price = prices.get(order.symbol)
            if price is None:
                self.orders[order.client_order_id] = replace(order, status="expired")
                continue
            self._fill(order, price, when)
        for symbol, pos in list(self.positions.items()):
            mark = prices.get(symbol, pos.market_value / pos.qty)
            self.positions[symbol] = replace(pos, market_value=pos.qty * mark)

    def mark(self, prices: Mapping[str, float]) -> None:
        """Revalue positions at new prices (as if a day went by)."""
        for symbol, pos in list(self.positions.items()):
            if symbol in prices:
                self.positions[symbol] = replace(pos, market_value=pos.qty * prices[symbol])

    # ------------------------------------------------------------------ helpers
    def _fill(self, order: BrokerOrder, price: float, when: datetime) -> None:
        if order.side == "buy":
            qty = (order.notional or 0.0) / price if order.qty is None else order.qty
            self.cash -= qty * price
            old = self.positions.get(order.symbol)
            total = qty + (old.qty if old else 0.0)
            cost = qty * price + (old.qty * old.avg_entry_price if old else 0.0)
            self.positions[order.symbol] = BrokerPosition(
                order.symbol, total, cost / total, total * price
            )
        else:
            held = self.positions.get(order.symbol)
            qty = min(order.qty or 0.0, held.qty if held else 0.0)
            if held is None or qty <= 0:
                self.orders[order.client_order_id] = replace(order, status="rejected")
                return
            self.cash += qty * price
            left = held.qty - qty
            if left <= 0:
                del self.positions[order.symbol]
            else:
                self.positions[order.symbol] = replace(held, qty=left, market_value=left * price)
        self.orders[order.client_order_id] = replace(
            order, status="filled", filled_qty=qty, filled_avg_price=price, filled_at=when
        )

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail:
            raise BrokerError(f"fake broker: {method} failed (test switch)")


def _next_weekday(moment: datetime) -> datetime:
    moment += timedelta(days=1)
    while moment.weekday() >= WEEKEND:
        moment += timedelta(days=1)
    return moment
