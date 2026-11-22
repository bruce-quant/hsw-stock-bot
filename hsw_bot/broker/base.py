"""What the bot needs from a broker, and nothing more.

The bot talks to the broker only through the ``Broker`` interface below. Two
classes implement it:

* ``AlpacaBroker`` (``alpaca.py``): the real thing, paper or live;
* ``FakeBroker`` (``fake.py``): an in memory pretend broker for tests and
  dry runs. It needs no keys and never touches the internet.

Because the bot only knows this interface, we can test every safety rule with
the fake broker before a real dollar is ever at risk.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

OrderSide = Literal["buy", "sell"]

# Order states. "Open" means the broker may still fill it.
OPEN_STATUSES = frozenset(
    {
        "new",
        "accepted",
        "pending_new",
        "partially_filled",
        "accepted_for_bidding",
        "pending_review",
        "held",
        "calculated",
        "pending_replace",
        "pending_cancel",
        "suspended",
        "stopped",
    }
)
FILLED = "filled"
FAILED_STATUSES = frozenset({"canceled", "expired", "rejected", "done_for_day", "replaced"})


class BrokerError(Exception):
    """The broker could not be reached or gave an answer we cannot use."""


class DuplicateOrderError(BrokerError):
    """The broker refused an order because its id was already used."""


MASK_KEEP = 4  # how many characters of an account number we show


@dataclass(frozen=True)
class AccountSnapshot:
    """The account numbers the safety checks need."""

    account_id: str  # the broker's id for this account; the records must match it
    account_number: str  # shown masked, e.g. ****1234
    equity: float  # cash plus the value of all positions
    last_equity: float  # equity at the previous trading day's close
    cash: float
    buying_power: float
    blocked: bool  # True if the broker blocked trading on this account
    status: str
    multiplier: float = 1.0  # buying power multiplier: above 1 means the account can borrow
    balance_asof: date | None = None  # the day last_equity is from

    @property
    def masked_number(self) -> str:
        """The account number with all but the last few characters hidden."""
        return mask(self.account_number)


def mask(text: str) -> str:
    """Hide all but the last few characters, for logs and screens."""
    return "****" + text[-MASK_KEEP:] if text else "****"


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float


@dataclass(frozen=True)
class BrokerOrder:
    """An order as the broker sees it."""

    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    status: str
    qty: float | None
    notional: float | None
    filled_qty: float
    filled_avg_price: float | None
    submitted_at: datetime | None
    filled_at: datetime | None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_filled(self) -> bool:
        return self.status == FILLED


@dataclass(frozen=True)
class OrderRequest:
    """A market order for the next open. Exactly one of ``qty`` or ``notional`` is set."""

    symbol: str
    side: OrderSide
    client_order_id: str
    qty: float | None = None
    notional: float | None = None

    def __post_init__(self) -> None:
        if (self.qty is None) == (self.notional is None):
            raise ValueError("An order needs exactly one of qty (shares) or notional (dollars).")


@dataclass(frozen=True)
class CashMovement:
    """Money (or shares) moved into or out of the account: a deposit, withdrawal or transfer.

    Moving money is not a gain or a loss, so the drawdown limit must not count
    it. Interest, dividends and fees are NOT movements: they are results.
    """

    movement_id: str  # the broker's id, so each movement is counted once
    day: date
    amount: float  # dollars: above 0 came in, below 0 went out
    kind: str  # the broker's activity type, for the journal (for example CSD or CSW)
    shares: bool = False  # True if shares moved, not cash (their value is not reliable)


@dataclass(frozen=True)
class MarketClock:
    """Now, and whether the market is open. Times are timezone aware."""

    now: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True)
class AssetInfo:
    symbol: str
    tradable: bool
    fractionable: bool


class Broker(Protocol):
    """Everything the bot asks of a broker."""

    @property
    def label(self) -> str:
        """For logs: "fake", "alpaca paper" or "alpaca LIVE"."""
        ...

    def get_account(self) -> AccountSnapshot: ...

    def get_positions(self) -> list[BrokerPosition]: ...

    def get_open_orders(self) -> list[BrokerOrder]: ...

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        """The order with this id, or None if the broker never saw it."""
        ...

    def get_order_history(self, symbols: Sequence[str] | None = None) -> list[BrokerOrder]:
        """Every order (open and closed) for these symbols, oldest first."""
        ...

    def get_daily_equity(self, since: date) -> list[tuple[datetime, float]]:
        """The account value at the end of each trading day since ``since``.

        Each point is (the broker's label for that day, equity). The label is
        the START of the day's window; ``runner.session_of`` turns it into a
        trading day. Raises ``BrokerError`` if the broker cannot say.
        """
        ...

    def get_cash_movements(self, since: date) -> list[CashMovement]:
        """Deposits, withdrawals and transfers since ``since`` (a few days early is fine)."""
        ...

    def submit_order(self, request: OrderRequest) -> BrokerOrder: ...

    def get_clock(self) -> MarketClock: ...

    def last_session(self, now: datetime) -> date | None:
        """The most recent trading day whose close has passed, or None if unknown."""
        ...

    def get_asset(self, symbol: str) -> AssetInfo: ...
