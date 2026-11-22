"""The real broker: Alpaca, through the official ``alpaca-py`` package.

What the Alpaca docs say about fractional orders (checked on 2026-11-21):

* "Fractional trading" page (updated 2026-10-28): fractional orders work for
  market, limit, stop and stop limit orders, and only with
  ``time_in_force = day``. You may send a share count (``qty``) OR a dollar
  amount (``notional``), never both. Fractional short sales are not supported.
* "Placing orders" page (updated 2026-10-13): for fractional orders, only DAY
  is allowed; gtc, opg, cls, ioc and fok are not. A day order "submitted after
  the close ... is queued and submitted the following trading day", and
  "any market orders received before 9:28 will be filled at the Nasdaq
  Official Opening Price".
* API reference for POST /v2/orders (updated 2026-05-27): a fractional
  ``qty`` works only for market and day orders; ``notional`` only for market
  orders with day; ``client_order_id`` is at most 128 characters.
* Bracket, OCO, OTO and trailing stop orders are not listed as supported for
  fractional quantities.

How our exit design follows from that:

* We cannot rest a good till canceled stop or a bracket order on a fractional
  position. A day stop order would have to be sent again every morning, and
  it would then behave differently from the backtest.
* So the bot checks every exit rule on the daily close, exactly as the
  backtest does, and sends plain market DAY orders after the close. They wait
  in the queue and fill at the next open. Buys use ``notional`` (dollars);
  sells use ``qty`` (all the shares we hold).
* The price of that simplicity: a stock can gap far below its stop overnight.
  The backtest includes those gaps, because it also sells at the next open.

Symbols: Yahoo writes share classes with a dash (BRK-B); Alpaca uses a dot
(BRK.B). The bot works with Yahoo style inside and converts at this border.

Paper or live is chosen when the client is created. Paper is the default and
live needs two separate switches (see ``hsw_bot.cli``).

Timeouts: ``alpaca-py`` 0.45 has no timeout setting (checked in its source), and
a request without a timeout can wait forever on a dropped connection. So we
give its HTTP session a default timeout. When a request to SEND an order times
out, the bot then asks the broker by order id whether it arrived.

More Alpaca facts the bot relies on (all checked on 2026-11-21):

* Account (API reference, updated 2026-05-27): ``last_equity`` is "Equity as
  of previous trading day at 16:00:00 ET", and ``balance_asof`` is "The date
  of the snapshot for last_* fields". When that date rolls over is not
  documented, so the bot checks it (see ``runner.daily_loss_check``).
* Portfolio history (API reference, updated 2026-05-27): each timestamp is
  "left-labeled (the beginning of time window)", and equity is the value "as
  of the end of each time window". With ``timeframe=1D`` only trading days
  appear. The time zone of a daily label is not documented, so
  ``runner.session_of`` reads it in a way that works for every likely choice.
* Account activities (GET /v2/account/activities; API reference updated
  2026-05-27, type list updated 2026-10-16): CSD is "Cash deposit(+)", CSW is
  "Cash withdrawal(-)", JNLC "Journal entry (cash)", JNLS "Journal entry
  (stock)", ACATC and ACATS are "ACATS IN/OUT" for cash and securities, FOPT
  is "Free of Payment Transfer". ``net_amount`` is "positive or negative".
  At most 100 per page; the next page starts after ``page_token``.
* Account configurations (PATCH /v2/account/configurations, updated
  2026-05-27): no field is required, and ``max_margin_multiplier`` "Can be
  "1", "2", or "4"". So ``forbid_margin`` sends only that one field.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from alpaca.common.enums import Sort
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.models import (
    AccountConfiguration,
    Asset,
    Calendar,
    Clock,
    Order,
    PortfolioHistory,
    Position,
    TradeAccount,
)
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrdersRequest,
    GetPortfolioHistoryRequest,
    MarketOrderRequest,
)

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

log = logging.getLogger("hsw_bot")

MARKET_TZ = ZoneInfo("America/New_York")
NOT_FOUND = 404
HISTORY_PAGE = 500  # the most orders Alpaca returns per request
MAX_HISTORY_PAGES = 40  # 20,000 orders: far more than this bot will ever place
DAILY = "1D"
DAYS_PER_MONTH = 28  # a "1M" history always covers this many days
DAYS_PER_YEAR = 365
NO_MARGIN = "1"  # max_margin_multiplier "1" means the account cannot borrow
CONFIGURATIONS_PATH = "/account/configurations"
ACTIVITIES_PATH = "/account/activities"
ACTIVITY_PAGE = 100  # the most activities Alpaca returns per request
MAX_ACTIVITY_PAGES = 50
CASH_ACTIVITIES = ("CSD", "CSW", "JNL", "JNLC", "ACATC")  # money in or out
SHARE_ACTIVITIES = ("JNLS", "ACATS", "FOPT")  # shares in or out
CANCELED = "canceled"
CALENDAR_LOOKBACK_DAYS = 10
MAX_OPEN_ORDERS = 500
NOTIONAL_DECIMALS = 2  # Alpaca takes dollar amounts to the cent
QTY_DECIMALS = 9  # and share counts to 9 decimals
DUPLICATE_HINT = "client_order_id must be unique"


def to_alpaca_symbol(symbol: str) -> str:
    """BRK-B (Yahoo style) -> BRK.B (Alpaca style)."""
    return symbol.replace("-", ".")


def from_alpaca_symbol(symbol: str) -> str:
    """BRK.B (Alpaca style) -> BRK-B (Yahoo style)."""
    return symbol.replace(".", "-")


class AlpacaBroker:
    """Alpaca through ``alpaca-py``. Every call is wrapped so errors become ``BrokerError``."""

    def __init__(self, api_key: str, secret_key: str, *, paper: bool, timeout: float) -> None:
        self.paper = paper
        self._client = TradingClient(api_key, secret_key, paper=paper)
        session = getattr(self._client, "_session", None)
        if isinstance(session, requests.Session):
            self._client._session = TimeoutSession(timeout)
        else:  # a future alpaca-py changed its insides; say so instead of hanging silently
            log.warning("Could not set a request timeout on the Alpaca client.")

    @property
    def label(self) -> str:
        return "alpaca paper" if self.paper else "alpaca LIVE"

    def get_account(self) -> AccountSnapshot:
        account = self._call("read the account", self._client.get_account)
        if not isinstance(account, TradeAccount):
            raise BrokerError("Alpaca returned an account in an unexpected shape.")
        return account_from_alpaca(account)

    def get_positions(self) -> list[BrokerPosition]:
        positions = self._call("read positions", self._client.get_all_positions)
        return [position_from_alpaca(p) for p in _as_list(positions, Position)]

    def get_open_orders(self) -> list[BrokerOrder]:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=MAX_OPEN_ORDERS)
        orders = self._call("read open orders", lambda: self._client.get_orders(request))
        return [order_from_alpaca(o) for o in _as_list(orders, Order)]

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        try:
            order = self._client.get_order_by_client_id(client_order_id)
        except APIError as err:
            if _status(err) == NOT_FOUND:
                return None
            raise BrokerError(f"Could not look up order {client_order_id}: {err}") from err
        except Exception as err:  # network errors and the like
            raise BrokerError(f"Could not look up order {client_order_id}: {err}") from err
        if not isinstance(order, Order):
            raise BrokerError("Alpaca returned an order in an unexpected shape.")
        return order_from_alpaca(order)

    def get_order_history(self, symbols: Sequence[str] | None = None) -> list[BrokerOrder]:
        """Every order, newest page first from Alpaca, returned oldest first."""
        found: dict[str, BrokerOrder] = {}
        until: datetime | None = None
        names = [to_alpaca_symbol(s) for s in symbols] if symbols else None
        for _ in range(MAX_HISTORY_PAGES):
            request = GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                limit=HISTORY_PAGE,
                direction=Sort.DESC,
                until=until,
                symbols=names,
            )
            page = _as_list(self._history_page(request), Order)
            for order in page:
                found[str(order.id)] = order_from_alpaca(order)
            if len(page) < HISTORY_PAGE:
                break
            until = min(o.submitted_at or o.created_at for o in page)
        return sorted(found.values(), key=_order_time)

    def _history_page(self, request: GetOrdersRequest) -> Any:
        return self._call("read the order history", lambda: self._client.get_orders(request))

    def get_daily_equity(self, since: date) -> list[tuple[datetime, float]]:
        days = (datetime.now(MARKET_TZ).date() - since).days
        period = "1M" if days < DAYS_PER_MONTH else f"{math.ceil(days / DAYS_PER_YEAR)}A"
        request = GetPortfolioHistoryRequest(period=period, timeframe=DAILY)
        history = self._call(
            "read the portfolio history", lambda: self._client.get_portfolio_history(request)
        )
        if not isinstance(history, PortfolioHistory):
            raise BrokerError("Alpaca returned a portfolio history in an unexpected shape.")
        return daily_points(history, since)

    def get_cash_movements(self, since: date) -> list[CashMovement]:
        params: dict[str, Any] = {
            "activity_types": ",".join((*CASH_ACTIVITIES, *SHARE_ACTIVITIES)),
            "after": since.isoformat(),
            "direction": "asc",
            "page_size": ACTIVITY_PAGE,
        }
        found: list[CashMovement] = []
        for _ in range(MAX_ACTIVITY_PAGES):
            page = self._activities_page(dict(params))
            found += [m for m in (movement_from_alpaca(item) for item in page) if m is not None]
            if len(page) < ACTIVITY_PAGE:
                return found
            params["page_token"] = str(page[-1].get("id", ""))
        raise BrokerError("Alpaca returned more account activities than expected.")

    def _activities_page(self, params: dict[str, Any]) -> list[Mapping[str, Any]]:
        page = self._call(
            "read deposits and withdrawals", lambda: self._client.get(ACTIVITIES_PATH, params)
        )
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise BrokerError("Alpaca returned account activities in an unexpected shape.")
        return page

    def max_margin_multiplier(self) -> str:
        """The account's borrowing limit setting: "1" means no borrowing."""
        return str(self._configuration().max_margin_multiplier)

    def forbid_margin(self) -> dict[str, str]:
        """Change ONLY the max margin multiplier, to 1. Then read every setting back.

        Sending the whole settings object back (what ``set_account_configurations``
        does) would also send old or empty values for settings we never meant
        to touch. So we send one field, and show the owner the result.
        """
        self._call(
            "change the account settings",
            lambda: self._client.patch(CONFIGURATIONS_PATH, {"max_margin_multiplier": NO_MARGIN}),
        )
        return account_settings(self._configuration())

    def _configuration(self) -> AccountConfiguration:
        config = self._call("read the account settings", self._client.get_account_configurations)
        if not isinstance(config, AccountConfiguration):
            raise BrokerError("Alpaca returned account settings in an unexpected shape.")
        return config

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        alpaca_request = market_order_request(request)
        try:
            order = self._client.submit_order(alpaca_request)
        except APIError as err:
            if DUPLICATE_HINT in str(err):
                raise DuplicateOrderError(str(err)) from err
            raise BrokerError(f"Alpaca refused the order for {request.symbol}: {err}") from err
        except Exception as err:
            raise BrokerError(f"Could not send the order for {request.symbol}: {err}") from err
        if not isinstance(order, Order):
            raise BrokerError("Alpaca returned an order in an unexpected shape.")
        return order_from_alpaca(order)

    def get_clock(self) -> MarketClock:
        clock = self._call("read the market clock", self._client.get_clock)
        if not isinstance(clock, Clock):
            raise BrokerError("Alpaca returned a clock in an unexpected shape.")
        return clock_from_alpaca(clock)

    def last_session(self, now: datetime) -> date | None:
        start = (now - timedelta(days=CALENDAR_LOOKBACK_DAYS)).date()
        request = GetCalendarRequest(start=start, end=now.date())
        days = self._call("read the market calendar", lambda: self._client.get_calendar(request))
        return last_closed_session(_as_list(days, Calendar), now)

    def get_asset(self, symbol: str) -> AssetInfo:
        asset = self._call(
            f"read asset {symbol}", lambda: self._client.get_asset(to_alpaca_symbol(symbol))
        )
        if not isinstance(asset, Asset):
            raise BrokerError("Alpaca returned an asset in an unexpected shape.")
        return AssetInfo(symbol, bool(asset.tradable), bool(asset.fractionable))

    def _call(self, what: str, action: Any) -> Any:
        try:
            return action()
        except Exception as err:
            raise BrokerError(f"Could not {what} at Alpaca: {err}") from err


# ----------------------------------------------------------------------------- converters
# Plain functions, so the tests can check them with real alpaca-py objects offline.


def market_order_request(request: OrderRequest) -> MarketOrderRequest:
    """Our order -> an Alpaca market DAY order (the only kind fractional orders allow)."""
    side = OrderSide.BUY if request.side == "buy" else OrderSide.SELL
    common: dict[str, Any] = {
        "symbol": to_alpaca_symbol(request.symbol),
        "side": side,
        "time_in_force": TimeInForce.DAY,
        "client_order_id": request.client_order_id,
    }
    if request.notional is not None:
        return MarketOrderRequest(notional=round(request.notional, NOTIONAL_DECIMALS), **common)
    return MarketOrderRequest(qty=round(request.qty or 0.0, QTY_DECIMALS), **common)


def account_from_alpaca(account: TradeAccount) -> AccountSnapshot:
    blocked = bool(account.trading_blocked) or bool(account.account_blocked)
    status = getattr(account.status, "value", str(account.status))
    asof = account.balance_asof
    return AccountSnapshot(
        account_id=str(account.id),
        account_number=str(account.account_number or ""),
        equity=_number(account.equity),
        last_equity=_number(account.last_equity),
        cash=_number(account.cash),
        buying_power=_number(account.buying_power),
        blocked=blocked,
        status=str(status),
        multiplier=_number(account.multiplier) if account.multiplier is not None else 1.0,
        balance_asof=date.fromisoformat(str(asof)[:10]) if asof else None,
    )


def position_from_alpaca(position: Position) -> BrokerPosition:
    return BrokerPosition(
        symbol=from_alpaca_symbol(position.symbol),
        qty=_number(position.qty),
        avg_entry_price=_number(position.avg_entry_price),
        market_value=_number(position.market_value),
    )


def order_from_alpaca(order: Order) -> BrokerOrder:
    status = getattr(order.status, "value", str(order.status))
    side = getattr(order.side, "value", str(order.side))
    return BrokerOrder(
        order_id=str(order.id),
        client_order_id=order.client_order_id,
        symbol=from_alpaca_symbol(order.symbol or ""),
        side="buy" if side == "buy" else "sell",
        status=str(status),
        qty=_maybe_number(order.qty),
        notional=_maybe_number(order.notional),
        filled_qty=_number(order.filled_qty or 0),
        filled_avg_price=_maybe_number(order.filled_avg_price),
        submitted_at=order.submitted_at,
        filled_at=order.filled_at,
    )


def daily_points(history: PortfolioHistory, since: date) -> list[tuple[datetime, float]]:
    """(label, equity) for each day in a 1D portfolio history, from ``since`` on."""
    try:
        pairs = list(zip(history.timestamp, history.equity, strict=True))
    except (TypeError, ValueError) as err:
        raise BrokerError("Alpaca returned a portfolio history with uneven lists.") from err
    points: list[tuple[datetime, float]] = []
    for stamp, value in pairs:
        equity = _maybe_number(value)
        if equity is None or not math.isfinite(equity) or equity <= 0:
            continue  # days before the account had money in it
        label = datetime.fromtimestamp(int(stamp), UTC)
        if label.astimezone(MARKET_TZ).date() >= since - timedelta(days=1):
            points.append((label, equity))
    return points


def movement_from_alpaca(item: Mapping[str, Any]) -> CashMovement | None:
    """One account activity -> a deposit, withdrawal or transfer (None if it was canceled)."""
    kind = str(item.get("activity_type", ""))
    if kind not in (*CASH_ACTIVITIES, *SHARE_ACTIVITIES):
        return None
    if str(item.get("status") or "").lower() == CANCELED:
        return None
    if not item.get("id") or not item.get("date"):
        raise BrokerError(f"Alpaca returned a {kind} activity without an id or a date.")
    amount = _number(item.get("net_amount") or 0)
    if not math.isfinite(amount):
        raise BrokerError(f"Alpaca returned a {kind} activity with an amount of {amount!r}.")
    return CashMovement(
        movement_id=str(item["id"]),
        day=date.fromisoformat(str(item["date"])[:10]),
        amount=amount,
        kind=kind,
        shares=kind in SHARE_ACTIVITIES or bool(item.get("symbol")),
    )


def account_settings(config: AccountConfiguration) -> dict[str, str]:
    """Every account setting the broker reported, as plain text, to show the owner."""
    shown: dict[str, str] = {}
    for name, value in config.model_dump().items():
        if value is not None:
            shown[name] = str(getattr(value, "value", value))
    return shown


def clock_from_alpaca(clock: Clock) -> MarketClock:
    return MarketClock(
        now=clock.timestamp,
        is_open=bool(clock.is_open),
        next_open=clock.next_open,
        next_close=clock.next_close,
    )


def last_closed_session(days: list[Calendar], now: datetime) -> date | None:
    """The newest trading day in the calendar whose close is already behind us."""
    closed = [d.date for d in days if d.close.replace(tzinfo=MARKET_TZ) <= now]
    return max(closed) if closed else None


class TimeoutSession(requests.Session):
    """An HTTP session that gives every request a default timeout."""

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self.default_timeout = timeout

    def request(self, method: str | bytes, url: str | bytes, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self.default_timeout)
        return super().request(method, url, *args, **kwargs)


def _order_time(order: BrokerOrder) -> datetime:
    return order.submitted_at or datetime.min.replace(tzinfo=MARKET_TZ)


def _as_list(value: Any, kind: type) -> list[Any]:
    if not isinstance(value, list) or not all(isinstance(v, kind) for v in value):
        raise BrokerError(f"Alpaca returned {kind.__name__} data in an unexpected shape.")
    return value


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as err:
        raise BrokerError(f"Alpaca returned {value!r} where a number was expected.") from err


def _maybe_number(value: Any) -> float | None:
    return None if value is None else _number(value)


def _status(err: APIError) -> int | None:
    try:
        code = err.status_code
    except Exception:
        return None
    return code if isinstance(code, int) else None
