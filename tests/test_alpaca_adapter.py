"""The Alpaca adapter, checked offline with real ``alpaca-py`` objects.

We have no keys in the tests, so nothing here talks to Alpaca. Instead we
build the same objects ``alpaca-py`` would build from Alpaca's answers, and
check that our converters read them correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import Calendar, Clock, Order, Position, TradeAccount

from hsw_bot.broker.alpaca import (
    account_from_alpaca,
    clock_from_alpaca,
    from_alpaca_symbol,
    last_closed_session,
    market_order_request,
    order_from_alpaca,
    position_from_alpaca,
    to_alpaca_symbol,
)
from hsw_bot.broker.base import OrderRequest

ID = "904837e3-3b76-47ec-b432-046db621571b"


def order_payload(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": ID,
        "client_order_id": "hsw-swing-20261120-BRK-B-buy",
        "created_at": "2026-11-20T23:00:00Z",
        "updated_at": "2026-11-20T23:00:00Z",
        "submitted_at": "2026-11-20T23:00:00Z",
        "filled_at": None,
        "expired_at": None,
        "canceled_at": None,
        "failed_at": None,
        "replaced_at": None,
        "replaced_by": None,
        "replaces": None,
        "asset_id": ID,
        "symbol": "BRK.B",
        "asset_class": "us_equity",
        "notional": "1234.56",
        "qty": None,
        "filled_qty": "0",
        "filled_avg_price": None,
        "order_class": "simple",
        "order_type": "market",
        "type": "market",
        "side": "buy",
        "time_in_force": "day",
        "limit_price": None,
        "stop_price": None,
        "status": "accepted",
        "extended_hours": False,
        "legs": None,
        "trail_percent": None,
        "trail_price": None,
        "hwm": None,
    }
    payload.update(changes)
    return payload


def test_symbols_convert_between_yahoo_and_alpaca_style() -> None:
    assert to_alpaca_symbol("BRK-B") == "BRK.B"
    assert from_alpaca_symbol("BRK.B") == "BRK-B"
    assert to_alpaca_symbol("AAPL") == "AAPL"


def test_buys_are_dollar_market_day_orders() -> None:
    request = market_order_request(OrderRequest("BRK-B", "buy", "hsw-x", notional=1234.5678))
    assert request.symbol == "BRK.B"
    assert request.notional == 1234.57
    assert request.qty is None
    assert request.side == OrderSide.BUY
    assert request.time_in_force == TimeInForce.DAY


def test_sells_are_share_market_day_orders() -> None:
    request = market_order_request(OrderRequest("AAPL", "sell", "hsw-y", qty=3.1234567891))
    assert request.qty == pytest.approx(3.123456789)
    assert request.notional is None
    assert request.side == OrderSide.SELL
    assert request.time_in_force == TimeInForce.DAY
    assert request.client_order_id == "hsw-y"


def test_order_request_needs_exactly_one_size() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        OrderRequest("AAPL", "buy", "id", qty=1.0, notional=10.0)
    with pytest.raises(ValueError, match="exactly one"):
        OrderRequest("AAPL", "buy", "id")


def test_accepted_order_is_not_a_fill() -> None:
    order = order_from_alpaca(Order(**order_payload()))
    assert order.symbol == "BRK-B"
    assert order.status == "accepted"
    assert order.is_open and not order.is_filled
    assert order.filled_qty == 0.0 and order.filled_avg_price is None
    assert order.notional == pytest.approx(1234.56)


def test_filled_order_reports_the_broker_numbers() -> None:
    filled = Order(
        **order_payload(
            status="filled",
            filled_qty="2.4691",
            filled_avg_price="500.0",
            filled_at="2026-11-23T14:30:01Z",
        )
    )
    order = order_from_alpaca(filled)
    assert order.is_filled and not order.is_open
    assert order.filled_qty == pytest.approx(2.4691)
    assert order.filled_avg_price == pytest.approx(500.0)
    assert order.filled_at == datetime(2026, 11, 23, 14, 30, 1, tzinfo=UTC)


def test_account_and_position_numbers() -> None:
    account_payload: dict[str, Any] = {
        "id": ID,
        "account_number": "PA0000000",
        "status": "ACTIVE",
        "currency": "USD",
        "cash": "2500.50",
        "equity": "10000.25",
        "last_equity": "9900.00",
        "buying_power": "20000",
        "trading_blocked": False,
        "account_blocked": False,
        "pattern_day_trader": False,
        "transfers_blocked": False,
        "trade_suspended_by_user": False,
        "shorting_enabled": False,
        "multiplier": "2",
        "created_at": "2026-01-01T00:00:00Z",
    }
    account = TradeAccount(**account_payload)
    snap = account_from_alpaca(account)
    assert (snap.equity, snap.last_equity, snap.cash) == (10000.25, 9900.0, 2500.5)
    assert not snap.blocked and snap.status == "ACTIVE"

    position_payload: dict[str, Any] = {
        "asset_id": ID,
        "symbol": "BRK.B",
        "exchange": "NYSE",
        "asset_class": "us_equity",
        "avg_entry_price": "480.10",
        "qty": "1.5",
        "side": "long",
        "market_value": "750.00",
        "cost_basis": "720.15",
        "qty_available": "1.5",
    }
    position = Position(**position_payload)
    held = position_from_alpaca(position)
    assert held.symbol == "BRK-B" and held.qty == 1.5 and held.market_value == 750.0


def test_clock_and_calendar() -> None:
    clock_payload: dict[str, Any] = {
        "timestamp": "2026-11-20T18:00:00-05:00",
        "is_open": False,
        "next_open": "2026-11-23T09:30:00-05:00",
        "next_close": "2026-11-23T16:00:00-05:00",
    }
    clock = Clock(**clock_payload)
    snap = clock_from_alpaca(clock)
    assert not snap.is_open and snap.next_open > snap.now
    days = [
        Calendar(date="2026-11-19", open="09:30", close="16:00"),
        Calendar(date="2026-11-20", open="09:30", close="16:00"),
    ]
    assert str(last_closed_session(days, snap.now)) == "2026-11-20"
    lunchtime = datetime.fromisoformat("2026-11-20T12:00:00-05:00")
    assert str(last_closed_session(days, lunchtime)) == "2026-11-19"


def test_account_identity_margin_and_balance_date() -> None:
    payload: dict[str, Any] = {
        "id": ID,
        "account_number": "PA3XYZ1234",
        "status": "ACTIVE",
        "currency": "USD",
        "cash": "100",
        "equity": "100",
        "last_equity": "99",
        "buying_power": "200",
        "multiplier": "2",
        "balance_asof": "2026-11-19",
        "trading_blocked": False,
        "account_blocked": False,
        "pattern_day_trader": False,
        "transfers_blocked": False,
        "trade_suspended_by_user": False,
        "shorting_enabled": False,
        "created_at": "2026-01-01T00:00:00Z",
    }
    snap = account_from_alpaca(TradeAccount(**payload))
    assert snap.account_id == ID
    assert snap.masked_number == "****1234"
    assert snap.multiplier == 2.0
    assert str(snap.balance_asof) == "2026-11-19"


def test_every_request_gets_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    from hsw_bot.broker.alpaca import AlpacaBroker, TimeoutSession

    broker = AlpacaBroker("PKTEST", "SECRET", paper=True, timeout=7.5)
    session = broker._client._session
    assert isinstance(session, TimeoutSession) and session.default_timeout == 7.5
    seen: dict[str, Any] = {}

    def fake_request(self: object, method: str, url: str, *args: Any, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(requests.Session, "request", fake_request)
    session.request("GET", "https://example.invalid")
    assert seen["timeout"] == 7.5


def test_order_history_pages_back_in_time() -> None:
    from hsw_bot.broker.alpaca import HISTORY_PAGE, AlpacaBroker

    def order_at(n: int) -> Order:
        stamp = f"2026-11-{1 + n // 1000:02d}T{(n // 60) % 24:02d}:{n % 60:02d}:00Z"
        return Order(
            **order_payload(
                id=f"00000000-0000-0000-0000-{n:012d}",
                submitted_at=stamp,
                created_at=stamp,
                client_order_id=f"hsw-swing-20261120-A{n}-buy",
            )
        )

    pages = [
        [order_at(n) for n in range(HISTORY_PAGE + 3, 3, -1)],
        [order_at(n) for n in (3, 2, 1)],
    ]
    asked: list[object] = []

    class Stub:
        def get_orders(self, request: object) -> list[Order]:
            asked.append(request)
            return pages[len(asked) - 1]

    broker = AlpacaBroker("PKTEST", "SECRET", paper=True, timeout=5.0)
    broker._client = Stub()  # type: ignore[assignment]
    history = broker.get_order_history()
    assert len(asked) == 2
    assert len(history) == HISTORY_PAGE + 3
    times = [o.submitted_at for o in history]
    assert times == sorted(t for t in times if t is not None), "oldest first"


def test_no_margin_sends_only_that_one_setting() -> None:
    """The old way sent the whole settings object back, with empty values for settings
    Alpaca no longer reports. Now only max_margin_multiplier goes out, then all are read back."""
    from alpaca.trading.models import AccountConfiguration

    from hsw_bot.broker.alpaca import AlpacaBroker

    current: dict[str, Any] = {
        "trade_confirm_email": "all",
        "suspend_trade": False,
        "no_shorting": True,
        "fractional_trading": True,
        "max_margin_multiplier": "2",
        "max_options_trading_level": 2,
        "ptp_no_exception_entry": False,
    }
    sent: list[tuple[str, Any]] = []

    class Stub:
        def get_account_configurations(self) -> AccountConfiguration:
            return AccountConfiguration(**current)

        def patch(self, path: str, data: Any = None) -> dict[str, Any]:
            sent.append((path, data))
            current["max_margin_multiplier"] = "1"
            return dict(current)

    broker = AlpacaBroker("PKTEST", "SECRET", paper=True, timeout=5.0)
    broker._client = Stub()  # type: ignore[assignment]
    settings = broker.forbid_margin()
    assert sent == [("/account/configurations", {"max_margin_multiplier": "1"})]
    assert settings["max_margin_multiplier"] == "1"
    assert settings["no_shorting"] == "True", "the other settings are shown, not changed"


def test_deposits_and_withdrawals_are_read_page_by_page() -> None:
    from datetime import date

    from hsw_bot.broker.alpaca import ACTIVITY_PAGE, AlpacaBroker

    def activity(n: int, kind: str = "CSD", amount: str = "100", **extra: Any) -> dict[str, Any]:
        item = {"id": f"2026111{n:06d}::x", "activity_type": kind, "date": "2026-11-18"}
        return {**item, "net_amount": amount, "status": "executed", **extra}

    first = [activity(n) for n in range(ACTIVITY_PAGE)]
    second = [
        activity(900, "CSW", "-250.5"),
        activity(901, "CSD", "75", status="canceled"),
        activity(902, "JNLS", "0", symbol="AAPL", qty="3"),
    ]
    asked: list[dict[str, Any]] = []

    class Stub:
        def get(self, path: str, data: Any = None) -> list[dict[str, Any]]:
            assert path == "/account/activities"
            asked.append(dict(data))
            return first if len(asked) == 1 else second

    broker = AlpacaBroker("PKTEST", "SECRET", paper=True, timeout=5.0)
    broker._client = Stub()  # type: ignore[assignment]
    found = broker.get_cash_movements(date(2026, 11, 11))
    assert len(asked) == 2 and asked[1]["page_token"] == first[-1]["id"]
    assert "CSW" in asked[0]["activity_types"] and asked[0]["after"] == "2026-11-11"
    assert len(found) == ACTIVITY_PAGE + 2, "the canceled deposit is left out"
    withdrawal, shares = found[-2], found[-1]
    assert (withdrawal.amount, withdrawal.kind, withdrawal.shares) == (-250.5, "CSW", False)
    assert shares.shares and shares.kind == "JNLS"


def test_daily_points_from_the_portfolio_history() -> None:
    from datetime import date

    from alpaca.trading.models import PortfolioHistory

    from hsw_bot.broker.alpaca import daily_points

    history = PortfolioHistory(
        timestamp=[1763355600, 1763442000, 1763528400],  # 2025-11-17/18/19 00:00 New York
        equity=[0.0, 10_000.0, 10_250.0],
        profit_loss=[0.0, 0.0, 250.0],
        profit_loss_pct=[0.0, 0.0, 0.025],
        base_value=10_000.0,
        timeframe="1D",
    )
    points = daily_points(history, date(2025, 11, 1))
    assert [value for _, value in points] == [10_000.0, 10_250.0], "a zero day is skipped"
    assert points[0][0] == datetime(2025, 11, 18, 5, 0, tzinfo=UTC)
