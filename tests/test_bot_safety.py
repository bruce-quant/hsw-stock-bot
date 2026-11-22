"""The bot's safety rules, each checked against the fake broker.

These tests are the reason we can say "fails closed" and mean it. Every one
runs the real bot code (``runner.run_once``); only the broker, the clock and
the market are pretend. Many of them reproduce a specific way a trading bot
can hurt its owner, and check that this one does not.
"""

from __future__ import annotations

import io
import json
import math
from collections.abc import Sequence
from contextlib import redirect_stdout
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from hsw_bot import planner, runner
from hsw_bot.broker.base import (
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    CashMovement,
    MarketClock,
    OrderRequest,
)
from hsw_bot.broker.fake import FakeBroker
from hsw_bot.cli import print_orders
from hsw_bot.data import Bars, to_float
from hsw_bot.engine import Sleeve, run_sleeves
from hsw_bot.journal import Journal
from hsw_bot.planner import AccountPlan, SleeveDay
from hsw_bot.rebuild import RebuildError, parse_order_id, rebuild_state
from hsw_bot.runner import BotContext, ExitCode, Mode, RunReport, run_once, session_of
from hsw_bot.state import SUBMITTING, BotState, CarriedSell, load_state, save_state
from hsw_bot.strategies import build_strategy
from tests import botkit

NEW_YORK = ZoneInfo("America/New_York")


@pytest.fixture
def day(bars: Bars) -> pd.Timestamp:
    return botkit.busy_day(bars)


def sells(report: RunReport) -> list[tuple[str, float | None]]:
    return [(o.plan.symbol, o.plan.qty) for o in report.orders if o.plan.side == "sell"]


def buys(report: RunReport) -> list[str]:
    return [o.plan.symbol for o in report.orders if o.plan.side == "buy"]


def sent_sells(broker: FakeBroker) -> list[BrokerOrder]:
    return [o for o in broker.orders.values() if o.side == "sell"]


def previous_day(bars: Bars, day: pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(bars.dates[list(bars.dates).index(day) - 1])


def update_state(config: Any, **changes: Any) -> BotState:
    """Change the saved records in place (for setting up a test)."""
    state = load_state(botkit.state_path(config))
    assert state is not None
    for name, value in changes.items():
        setattr(state, name, value)
    save_state(state, botkit.state_path(config))
    return state


def fill(cid: str, symbol: str, side: str, qty: float, price: float, when: datetime) -> BrokerOrder:
    """A filled order in the broker's history (yours or the bot's, by its id)."""
    assert side in ("buy", "sell")
    return BrokerOrder(f"id-{cid}", cid, symbol, side, "filled", qty, None, qty, price, when, when)  # type: ignore[arg-type]


def at_open(day: pd.Timestamp) -> datetime:
    return botkit.evening(day).replace(hour=9, minute=31)


# ----------------------------------------------------------------------------- the happy path


def test_paper_run_sends_orders_and_reads_them_back(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    report = botkit.run(config, broker, bars, day)
    assert report.exit_code is ExitCode.OK
    assert report.orders, "the busy day should produce orders"
    assert all(o.status == "accepted" for o in broker.orders.values())
    events = botkit.journal_events(config)
    for needed in ("run_start", "first_run", "status", "decision", "order_sent", "read_back"):
        assert needed in events
    state = load_state(botkit.state_path(config))
    assert state is not None
    assert state.account_id == botkit.FAKE_ACCOUNT
    assert set(state.pending) == set(broker.orders)
    assert all(p.status == "sent" for p in state.pending.values())
    assert not state.positions, "an accepted order is not a position yet"


def test_bot_orders_match_the_backtest(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    """One code path: with both strategies sharing the account, the bot's orders on day T
    are exactly the backtest's fills at the open of day T+1."""
    config = botkit.make_config(tmp_path)
    sleeves = [
        Sleeve(name, build_strategy(config, name), config.strategy(name).sizing, share)
        for name, share in config.bot.sleeves.items()
    ]
    after = bars.dates[list(bars.dates).index(day) + 1]
    result = run_sleeves(sleeves, bars.until(after), config.costs, 10_000.0, start=day)
    expected = {(f.symbol, round(f.qty * f.price, 2)) for f in result.fills if f.date == after}
    report = botkit.run(config, botkit.make_broker(day), bars, day)
    got = {(o.plan.symbol, o.plan.notional) for o in report.orders}
    assert expected
    assert got == expected


def test_second_run_the_same_evening_sends_nothing(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    first = botkit.run(config, broker, bars, day, run_id="first")
    sent = len(broker.orders)
    second = botkit.run(config, broker, bars, day, run_id="second")
    assert first.orders and sent == len(first.orders)
    assert len(broker.orders) == sent, "a second run must not double buy"
    assert second.exit_code is ExitCode.OK
    assert not second.results


def test_fills_are_read_back_on_the_next_run(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    botkit.run(config, broker, bars, day, run_id="first")
    after = botkit.next_open(broker, bars, day)
    botkit.run(config, broker, bars, after, run_id="second")
    state = load_state(botkit.state_path(config))
    assert state is not None and state.positions, "filled buys must become positions"
    for symbol, record in state.positions.items():
        held = broker.positions[symbol]
        assert record.qty == pytest.approx(held.qty)
        assert record.entry_price == pytest.approx(held.avg_entry_price)
        assert record.entry_date == f"{after:%Y-%m-%d}"
        assert record.signal_date == f"{day:%Y-%m-%d}"
    assert "fill" in botkit.journal_events(config)


def test_dry_run_works_without_a_calendar(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day, paper_calendar=False)
    report = botkit.run(config, broker, bars, day, mode=Mode.DRY_RUN)
    assert report.exit_code is ExitCode.OK
    assert report.orders


def test_every_run_prints_a_status_line(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    report = botkit.run(config, broker, bars, day)
    assert report.status_line.startswith("PAPER | account ****0001 | kill switch off")
    assert "0 managed, 0 ignored, 0 unmanaged" in report.status_line
    out = io.StringIO()
    with redirect_stdout(out):
        print_orders(report, header="PAPER run")
    assert report.status_line in out.getvalue()


# ----------------------------------------------------------------------------- fail closed


@pytest.mark.parametrize("method", ["get_account", "get_positions", "get_open_orders", "get_clock"])
def test_unreadable_broker_fails_closed(
    tmp_path: Path, bars: Bars, day: pd.Timestamp, method: str
) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    broker.fail.add(method)
    report = botkit.run(config, broker, bars, day)
    assert report.exit_code is ExitCode.FAILED_CLOSED
    assert not broker.orders
    assert "fail_closed" in botkit.journal_events(config)


def test_invalid_equity_fails_closed(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day, cash=math.nan)
    assert botkit.run(config, broker, bars, day).exit_code is ExitCode.FAILED_CLOSED
    assert not broker.orders


def test_blocked_account_fails_closed(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    broker.blocked = True
    assert botkit.run(config, broker, bars, day).exit_code is ExitCode.FAILED_CLOSED


def test_stale_data_fails_closed(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    """The broker says a newer trading day has closed, but the data stops a day earlier."""
    config = botkit.make_config(tmp_path)
    later = bars.dates[list(bars.dates).index(day) + 1]
    broker = botkit.make_broker(later)
    report = botkit.run(config, broker, bars, day)  # data only up to `day`
    assert report.exit_code is ExitCode.FAILED_CLOSED
    assert "trading day" in report.error
    assert not broker.orders


def test_data_download_error_fails_closed(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    journal = Journal(config.bot.journal_file, "t", "paper")

    def broken_loader(symbols: object, now: object) -> Bars:
        raise RuntimeError("Yahoo is down")

    ctx = BotContext(
        config, broker, Mode.PAPER, broken_loader, journal, botkit.state_path(config), print
    )
    assert run_once(ctx).exit_code is ExitCode.FAILED_CLOSED
    assert not broker.orders


def test_market_open_fails_closed_for_paper(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    noon = botkit.evening(day).replace(hour=12)
    broker.now = lambda: noon
    assert botkit.run(config, broker, bars, day).exit_code is ExitCode.FAILED_CLOSED
    assert not broker.orders


@dataclass
class ClockMovesForward(FakeBroker):
    """Closed at the first look, open by the time the orders are about to go out."""

    clock_reads: int = 0

    def get_clock(self) -> MarketClock:
        self.clock_reads += 1
        clock = super().get_clock()
        return replace(clock, is_open=self.clock_reads > 1)


def test_market_opening_during_the_run_stops_the_orders(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    when = botkit.evening(day)
    broker = ClockMovesForward(
        cash=10_000.0,
        now=lambda: when,
        session=day.date(),
        balance_asof=(day - pd.offsets.BDay(1)).date(),
    )
    report = botkit.run(config, broker, bars, day)
    assert report.exit_code is ExitCode.FAILED_CLOSED
    assert not broker.orders
    assert broker.clock_reads == 2  # read once at the start, once again right before sending


def test_corrupt_records_fail_closed(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path)
    path = botkit.state_path(config)
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json", encoding="utf-8")
    broker = botkit.make_broker(day)
    assert botkit.run(config, broker, bars, day).exit_code is ExitCode.FAILED_CLOSED
    assert not broker.orders


def test_no_good_bar_at_all_fails_closed(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path)
    close = bars.close.copy()
    close.loc[day, :] = math.nan
    broken = Bars(bars.open, bars.high, bars.low, close, bars.volume)
    report = botkit.run(config, botkit.make_broker(day), broken, day)
    assert report.exit_code is ExitCode.FAILED_CLOSED
    assert "data source" in report.error


def test_repeated_failures_warn_that_positions_are_unprotected(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = botkit.make_broker(day)
    botkit.seed(config, broker, bars, day, {"S05": (10.0, 10.0)})
    broker.fail.add("get_clock")
    first = botkit.run(config, broker, bars, day, run_id="first")
    second = botkit.run(config, broker, bars, day, run_id="second")
    assert first.exit_code is second.exit_code is ExitCode.FAILED_CLOSED
    assert not first.warnings
    assert second.warnings and "NO stop protection" in second.warnings[0]
    state = load_state(botkit.state_path(config))
    assert state is not None and state.fail_streak == 2
    broker.fail.clear()
    botkit.run(config, broker, bars, day, run_id="third")
    state = load_state(botkit.state_path(config))
    assert state is not None and state.fail_streak == 0, "a good run resets the count"


# ----------------------------------------------------------------------------- the records


def test_extra_shares_you_added_are_never_sold(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    """The records say 10 shares; you bought 50 more by hand. The exit sells only the 10."""
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = botkit.make_broker(day, cash=20_000.0)
    botkit.seed(config, broker, bars, day, {"S05": (10.0, 60.0)})
    report = botkit.run(config, broker, bars, day)
    assert sells(report) == [("S05", 10.0)]
    assert "S05" not in buys(report)
    assert report.exit_code is ExitCode.NEEDS_ATTENTION
    assert any("extra shares" in item for item in report.attention)
    assert "mismatch" in botkit.journal_events(config)


def test_fewer_shares_at_the_broker_sells_only_what_is_there(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = botkit.make_broker(day)
    botkit.seed(config, broker, bars, day, {"S05": (10.0, 7.0)})
    report = botkit.run(config, broker, bars, day)
    assert sells(report) == [("S05", 7.0)]
    assert report.exit_code is ExitCode.NEEDS_ATTENTION


def test_paper_and_live_keep_separate_records(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    """Paper records must never be applied to a live account."""
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    paper = botkit.make_broker(day)
    botkit.seed(config, paper, bars, day, {"S05": (40.0, 40.0)}, peak=100_000.0)
    live = botkit.make_broker(day, cash=8_000.0)
    live.positions["S05"] = BrokerPosition(
        "S05", 5.0, 100.0, 5 * to_float(bars.close.at[day, "S05"])
    )
    report = botkit.run(config, live, bars, day, mode=Mode.LIVE)
    assert report.exit_code is ExitCode.FAILED_CLOSED  # live has no records, but holds a stock
    assert "rebuild-state" in report.error
    assert not live.orders
    assert not config.bot.kill_switch_file.exists()
    assert botkit.state_path(config, Mode.PAPER).exists()
    assert not botkit.state_path(config, Mode.LIVE).exists()


def test_records_from_another_account_fail_closed(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    save_state(
        BotState(account_id="someone-else-9999", peak_equity=10_000.0), botkit.state_path(config)
    )
    broker = botkit.make_broker(day)
    report = botkit.run(config, broker, bars, day)
    assert report.exit_code is ExitCode.FAILED_CLOSED
    assert "****9999" in report.error and "****0001" in report.error
    assert not broker.orders


@pytest.mark.parametrize("what", ["positions", "open orders"])
def test_lost_records_with_holdings_fail_closed(
    tmp_path: Path, bars: Bars, day: pd.Timestamp, what: str
) -> None:
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = botkit.make_broker(day)
    if what == "positions":
        botkit.seed(config, broker, bars, day, {"S05": (0.0, 10.0)}, write=False)
    else:
        broker.submit_order(
            OrderRequest("S07", "buy", "hsw-swing-20200101-S07-buy", notional=500.0)
        )
    report = botkit.run(config, broker, bars, day)
    assert report.exit_code is ExitCode.FAILED_CLOSED
    assert "hsw-bot rebuild-state" in report.error
    assert not botkit.state_path(config).exists(), "no fresh records over a lost account"


@pytest.mark.parametrize("peak", [None, 0, True, math.nan, -5.0])
def test_a_broken_best_level_fails_closed(
    tmp_path: Path, bars: Bars, day: pd.Timestamp, peak: object
) -> None:
    config = botkit.make_config(tmp_path)
    path = botkit.state_path(config)
    path.parent.mkdir(parents=True)
    payload = {
        "schema": 2,
        "account_id": botkit.FAKE_ACCOUNT,
        "positions": {},
        "pending": {},
        "carried_sells": {},
        "sent_ids": [],
        "own_symbols": [],
        "peak_equity": peak,
        "drawdown_tripped": False,
        "fail_streak": 0,
        "last_run_utc": None,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    broker = botkit.make_broker(day)
    report = botkit.run(config, broker, bars, day)
    assert report.exit_code is ExitCode.FAILED_CLOSED
    assert not broker.orders


def test_rebuild_state_from_the_order_history(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    botkit.run(config, broker, bars, day, run_id="first")
    after = botkit.next_open(broker, bars, day)
    bought = {s: p.qty for s, p in broker.positions.items()}
    broker.positions["ZZZ"] = BrokerPosition("ZZZ", 3.0, 10.0, 30.0)  # bought by hand
    broker.daily_equity = [(botkit.evening(after), 12_345.0)]
    rebuilt = rebuild_state(broker, "hsw", peak=None, peak_now=True, fresh_start=False)
    records = rebuilt.state.positions
    assert {s: r.qty for s, r in records.items()} == pytest.approx(bought)
    for symbol, record in records.items():
        assert record.signal_date == f"{day:%Y-%m-%d}"
        assert record.sleeve in config.bot.sleeves
        assert record.entry_price == pytest.approx(broker.positions[symbol].avg_entry_price)
    claimed = {r.symbol: r.claimed for r in rebuilt.replays}
    assert claimed["ZZZ"] is False and all(claimed[s] for s in bought)
    assert rebuilt.state.peak_equity == pytest.approx(broker.get_account().equity)
    assert rebuilt.highest == (after.date(), 12_345.0), "shown as information only"
    assert rebuilt.state.account_id == botkit.FAKE_ACCOUNT


def test_rebuild_needs_a_best_level_chosen_by_you(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    broker = botkit.make_broker(day)
    with pytest.raises(RebuildError, match="--peak-now"):
        rebuild_state(broker, "hsw", peak=None, peak_now=False, fresh_start=False)
    with pytest.raises(RebuildError, match="not both"):
        rebuild_state(broker, "hsw", peak=12_000.0, peak_now=True, fresh_start=False)
    for bad in (-5.0, 0.0, math.nan):
        with pytest.raises(RebuildError, match="cannot be used"):
            rebuild_state(broker, "hsw", peak=bad, peak_now=False, fresh_start=False)
    chosen = rebuild_state(broker, "hsw", peak=12_000.0, peak_now=False, fresh_start=False)
    assert chosen.state.peak_equity == 12_000.0
    today = rebuild_state(broker, "hsw", peak=None, peak_now=True, fresh_start=False)
    assert today.state.peak_equity == pytest.approx(10_000.0)


def your_own_shares(config: Any, bars: Bars, day: pd.Timestamp) -> FakeBroker:
    """The bot bought 10 S05 long ago. You sold them by hand, so the records dropped
    them. Weeks later you bought 30 S05 for yourself."""
    broker = botkit.make_broker(day, cash=20_000.0)
    index = list(bars.dates).index(day)
    signal, entry, sold, rebought = (bars.dates[index - k] for k in (41, 40, 30, 3))
    bot_buy = f"hsw-swing-{signal:%Y%m%d}-S05-buy"
    broker.orders[bot_buy] = fill(bot_buy, "S05", "buy", 10.0, 100.0, at_open(entry))
    broker.orders["by-hand-1"] = fill("by-hand-1", "S05", "sell", 10.0, 105.0, at_open(sold))
    broker.orders["by-hand-2"] = fill("by-hand-2", "S05", "buy", 30.0, 98.0, at_open(rebought))
    price = to_float(bars.close.at[day, "S05"])
    broker.positions["S05"] = BrokerPosition("S05", 30.0, 98.0, 30 * price)
    broker.cash -= 30 * price
    state = BotState(
        account_id=botkit.FAKE_ACCOUNT, peak_equity=broker.get_account().equity, sent_ids=[bot_buy]
    )
    save_state(state, botkit.state_path(config))
    return broker


def test_your_own_shares_are_never_taken_over(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    """A run never takes a position from the order history, even in a stock the bot once had."""
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = your_own_shares(config, bars, day)
    before = len(broker.orders)
    report = botkit.run(config, broker, bars, day)
    assert "S05" not in {o.plan.symbol for o in report.orders}
    assert not [o for o in list(broker.orders.values())[before:] if o.symbol == "S05"]
    assert report.exit_code is ExitCode.NEEDS_ATTENTION
    assert any("S05" in item for item in report.attention)
    state = load_state(botkit.state_path(config))
    assert state is not None and "S05" not in state.positions
    assert "adopted" not in botkit.journal_events(config)


def test_rebuild_never_claims_your_own_shares(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = your_own_shares(config, bars, day)
    rebuilt = rebuild_state(broker, "hsw", peak=None, peak_now=True, fresh_start=False)
    assert not rebuilt.state.positions
    (replay,) = rebuilt.replays
    assert replay.symbol == "S05" and not replay.claimed and replay.bot == 0.0
    assert "sold" in replay.why


def test_rebuild_does_not_claim_a_stock_you_traded_after_the_bot(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    """The bot bought 10, you bought 5, then you sold 5. Whose 5? The rebuild does not guess."""
    broker = botkit.make_broker(day, cash=20_000.0)
    index = list(bars.dates).index(day)
    first, second, third = (bars.dates[index - k] for k in (30, 20, 10))
    bot_buy = f"hsw-swing-{bars.dates[index - 31]:%Y%m%d}-S05-buy"
    broker.orders[bot_buy] = fill(bot_buy, "S05", "buy", 10.0, 100.0, at_open(first))
    broker.orders["by-hand-1"] = fill("by-hand-1", "S05", "buy", 5.0, 101.0, at_open(second))
    broker.orders["by-hand-2"] = fill("by-hand-2", "S05", "sell", 5.0, 102.0, at_open(third))
    broker.positions["S05"] = BrokerPosition("S05", 10.0, 100.0, 1_000.0)
    rebuilt = rebuild_state(broker, "hsw", peak=None, peak_now=True, fresh_start=False)
    (replay,) = rebuilt.replays
    assert not replay.claimed and "by hand" in replay.why
    assert not rebuilt.state.positions


def test_rebuild_caps_the_bot_at_what_the_account_held(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    """You sold the bot's 10 by hand and bought 30 of your own; then the bot bought 5.
    Only those last 5 are the bot's."""
    broker = botkit.make_broker(day, cash=20_000.0)
    index = list(bars.dates).index(day)
    days = [bars.dates[index - k] for k in (40, 30, 20, 10)]
    old_buy = f"hsw-swing-{bars.dates[index - 41]:%Y%m%d}-S05-buy"
    new_buy = f"hsw-swing-{bars.dates[index - 11]:%Y%m%d}-S05-buy"
    broker.orders[old_buy] = fill(old_buy, "S05", "buy", 10.0, 100.0, at_open(days[0]))
    broker.orders["by-hand-1"] = fill("by-hand-1", "S05", "sell", 10.0, 101.0, at_open(days[1]))
    broker.orders["by-hand-2"] = fill("by-hand-2", "S05", "buy", 30.0, 99.0, at_open(days[2]))
    broker.orders[new_buy] = fill(new_buy, "S05", "buy", 5.0, 98.0, at_open(days[3]))
    broker.positions["S05"] = BrokerPosition("S05", 35.0, 99.0, 3_500.0)
    rebuilt = rebuild_state(broker, "hsw", peak=None, peak_now=True, fresh_start=False)
    record = rebuilt.state.positions["S05"]
    assert record.qty == pytest.approx(5.0)
    assert record.entry_price == pytest.approx(98.0)
    assert record.signal_date == f"{bars.dates[index - 11]:%Y-%m-%d}"


def test_order_ids_carry_a_retry_number() -> None:
    first = parse_order_id("hsw-swing-20261120-BRK-B-sell", "hsw")
    again = parse_order_id("hsw-swing-20261123-BRK-B-sell-r2", "hsw")
    assert first is not None and again is not None
    assert (first.symbol, first.side, first.retry) == ("BRK-B", "sell", 0)
    assert (again.symbol, again.side, again.retry, again.signal_date) == (
        "BRK-B",
        "sell",
        2,
        "2026-11-23",
    )
    assert parse_order_id("hsw-swing-20261120-AAPL-sell-x2", "hsw") is None


def test_fresh_start_leaves_your_positions_alone(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = botkit.make_broker(day)
    botkit.seed(config, broker, bars, day, {"S05": (0.0, 10.0)}, write=False)
    rebuilt = rebuild_state(broker, "hsw", peak=None, peak_now=True, fresh_start=True)
    assert rebuilt.state.own_symbols == ["S05"] and not rebuilt.state.positions
    save_state(rebuilt.state, botkit.state_path(config))
    report = botkit.run(config, broker, bars, day)
    assert "S05" not in {o.plan.symbol for o in report.orders}
    assert "1 ignored, 0 unmanaged" in report.status_line
    assert not any("S05" in item for item in report.attention)


def test_old_records_from_an_earlier_version_are_not_ignored(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    """An old state/bot_state.json, and an empty account: no silent fresh start."""
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = botkit.make_broker(day)
    old = tmp_path / "state" / "bot_state.json"
    old.parent.mkdir(parents=True)
    raw = {"schema": 1, "positions": {}, "pending": {}, "sent_ids": [], "peak_equity": 50_000.0}
    old.write_text(json.dumps(raw), encoding="utf-8")
    report = botkit.run(config, broker, bars, day)
    assert report.exit_code is ExitCode.FAILED_CLOSED
    assert "--peak 50000.00" in report.error and "rebuild-state" in report.error
    assert not broker.orders
    assert not botkit.state_path(config).exists()


def test_removing_a_strategy_that_still_holds_stocks_fails_closed(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path, sleeves={"steady": 1.0})
    broker = botkit.make_broker(day)
    botkit.seed(config, broker, bars, day, {"S05": (10.0, 10.0)})
    report = botkit.run(config, broker, bars, day)
    assert report.exit_code is ExitCode.FAILED_CLOSED
    assert "swing = 0.0" in report.error
    assert not broker.orders


def test_a_zero_share_strategy_only_sells(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path, sleeves={"steady": 0.5, "swing": 0.0})
    broker = botkit.make_broker(day)
    botkit.seed(config, broker, bars, day, {"S05": (10.0, 10.0)})
    report = botkit.run(config, broker, bars, day)
    assert ("S05", 10.0) in sells(report)
    swing_buys = [o for o in report.orders if o.sleeve == "swing" and o.plan.side == "buy"]
    assert not swing_buys


# ----------------------------------------------------------------------------- sending


@dataclass
class PositionsFailAfterSending(FakeBroker):
    reads: int = 0

    def get_positions(self) -> list[BrokerPosition]:
        self.reads += 1
        if self.reads >= 2:
            raise BrokerError("connection reset")
        return super().get_positions()


def test_a_failure_after_sending_says_the_orders_were_sent(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    when = botkit.evening(day)
    broker = PositionsFailAfterSending(
        cash=10_000.0,
        now=lambda: when,
        session=day.date(),
        balance_asof=(day - pd.offsets.BDay(1)).date(),
    )
    report = botkit.run(config, broker, bars, day)
    assert broker.orders, "orders really were sent"
    assert report.exit_code is ExitCode.NEEDS_ATTENTION
    assert report.sent == len(broker.orders)
    assert f"{len(broker.orders)} order(s) WERE sent" in report.error
    out = io.StringIO()
    with redirect_stdout(out):
        print_orders(report, header="PAPER run")
    assert "No orders were sent" not in out.getvalue()
    assert "WERE sent" in out.getvalue()


@dataclass
class WatchesTheRecords(FakeBroker):
    """Checks, at the moment of sending, that the order is already in the records."""

    state_file: Path = Path()
    seen: list[str] = field(default_factory=list)

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        state = load_state(self.state_file)
        assert state is not None
        self.seen.append(state.pending[request.client_order_id].status)
        return super().submit_order(request)


def test_each_order_is_recorded_before_it_is_sent(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    when = botkit.evening(day)
    broker = WatchesTheRecords(
        cash=10_000.0,
        now=lambda: when,
        session=day.date(),
        balance_asof=(day - pd.offsets.BDay(1)).date(),
        state_file=botkit.state_path(config),
    )
    report = botkit.run(config, broker, bars, day)
    assert report.orders and broker.seen == [SUBMITTING] * len(report.orders)


@dataclass
class ConnectionDropsAfterSending(FakeBroker):
    """The order reaches the broker, but the answer is lost on the way back."""

    victim: str = ""

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        order = super().submit_order(request)
        if request.symbol == self.victim:
            raise BrokerError("Could not send the order: Connection aborted")
        return order


def test_a_lost_answer_is_checked_and_the_order_tracked(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    probe = botkit.run(botkit.make_config(tmp_path / "probe"), botkit.make_broker(day), bars, day)
    victim = next(o.plan.symbol for o in probe.orders if o.plan.side == "buy")
    config = botkit.make_config(tmp_path / "real")
    when = botkit.evening(day)
    broker = ConnectionDropsAfterSending(
        cash=10_000.0,
        now=lambda: when,
        session=day.date(),
        balance_asof=(day - pd.offsets.BDay(1)).date(),
        victim=victim,
    )
    first = botkit.run(config, broker, bars, day, run_id="first")
    state = load_state(botkit.state_path(config))
    assert state is not None
    assert victim in {p.symbol for p in state.pending.values()}
    assert first.sent == len(broker.orders)
    after = botkit.next_open(broker, bars, day)
    botkit.run(config, broker, bars, after, run_id="second")
    state = load_state(botkit.state_path(config))
    assert state is not None and victim in state.positions, "never an orphan position"


@dataclass
class BrokerGoesQuiet(FakeBroker):
    """Sending fails, and the broker does not answer the follow up question either."""

    quiet: bool = False

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        self.quiet = True
        raise BrokerError("timed out")

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        if self.quiet:
            raise BrokerError("timed out")
        return super().get_order(client_order_id)


def test_an_unclear_order_stays_pending_until_the_broker_answers(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    when = botkit.evening(day)
    broker = BrokerGoesQuiet(
        cash=10_000.0,
        now=lambda: when,
        session=day.date(),
        balance_asof=(day - pd.offsets.BDay(1)).date(),
    )
    first = botkit.run(config, broker, bars, day, run_id="first")
    assert first.exit_code is ExitCode.NEEDS_ATTENTION
    state = load_state(botkit.state_path(config))
    assert state is not None and state.pending
    assert all(p.status == SUBMITTING for p in state.pending.values())
    broker.quiet = False  # the next evening the broker answers: it never got them
    second = botkit.run(config, broker, bars, day, run_id="second")
    state = load_state(botkit.state_path(config))
    assert state is not None and not state.pending
    assert second.exit_code is ExitCode.OK


@dataclass
class RefusesSells(FakeBroker):
    """Refuses every sell while ``refusing`` is on, as a broker might on a bad night."""

    refusing: bool = True

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        if self.refusing and request.side == "sell":
            raise BrokerError("Alpaca refused the order for S05: 500 internal error")
        return super().submit_order(request)


def like(cls: type[FakeBroker], day: pd.Timestamp, **extra: Any) -> Any:
    """A broker of another class, set up like ``botkit.make_broker(day)``."""
    base = botkit.make_broker(day)
    return cls(cash=base.cash, now=base.now, session=base.session,
               balance_asof=base.balance_asof, **extra)  # fmt: skip


def test_a_refused_sell_is_sent_again_with_a_new_id(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = like(RefusesSells, day)
    botkit.seed(config, broker, bars, day, {"S05": (10.0, 10.0)})
    first = botkit.run(config, broker, bars, day, run_id="first")
    assert first.exit_code is ExitCode.NEEDS_ATTENTION
    assert any("S05" in item and "sends it again" in item for item in first.attention)
    state = load_state(botkit.state_path(config))
    assert state is not None and state.carried_sells["S05"].tries == 1
    original = next(o.plan.reason for o in first.orders if o.plan.side == "sell")
    assert state.carried_sells["S05"].reason == original, "it still says why it sells"
    broker.refusing = False  # the broker is fine again; you run the bot again the same evening
    second = botkit.run(config, broker, bars, day, run_id="second")
    (sell,) = sent_sells(broker)
    assert sell.symbol == "S05" and sell.qty == 10.0
    assert sell.client_order_id.endswith("-sell-r1")
    parsed = parse_order_id(sell.client_order_id, "hsw")
    assert parsed is not None and parsed.retry == 1
    assert [o.carried for o in second.orders if o.plan.side == "sell"] == [True]


@dataclass
class DeadNetwork(FakeBroker):
    """Sells time out, and so do questions about them, until the network comes back."""

    down: bool = True

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        if self.down and request.side == "sell":
            raise BrokerError("connection timed out")
        return super().submit_order(request)

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        if self.down and "-sell" in client_order_id:
            raise BrokerError("connection timed out")
        return super().get_order(client_order_id)


def test_an_unclear_sell_is_sent_again_once_the_broker_answers(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = like(DeadNetwork, day)
    botkit.seed(config, broker, bars, day, {"S05": (10.0, 10.0)})
    first = botkit.run(config, broker, bars, day, run_id="first")
    assert first.exit_code is ExitCode.NEEDS_ATTENTION and not sent_sells(broker)
    broker.down = False
    second = botkit.run(config, broker, bars, day, run_id="second")
    (sell,) = sent_sells(broker)
    assert sell.symbol == "S05" and sell.client_order_id.endswith("-r1")
    assert any("never reached the broker" in item for item in second.attention)
    state = load_state(botkit.state_path(config))
    original = next(o.plan.reason for o in first.orders if o.plan.side == "sell")
    assert state is not None and state.carried_sells["S05"].reason == original


def test_a_sell_that_keeps_failing_raises_a_loud_warning(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = like(RefusesSells, day)
    botkit.seed(config, broker, bars, day, {"S05": (10.0, 10.0)})
    reports = [botkit.run(config, broker, bars, day, run_id=f"run{n}") for n in range(3)]
    assert not reports[0].warnings and not reports[1].warnings
    assert reports[2].warnings and "failed 3 times" in reports[2].warnings[0]
    assert "NO working exit" in reports[2].warnings[0]
    state = load_state(botkit.state_path(config))
    assert (
        state is not None and state.carried_sells["S05"].tries == config.bot.warn_after_failed_sells
    )


def test_a_carried_sell_goes_out_even_with_a_bad_bar(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    """A market sell for the next open needs no fresh price, so a bad bar does not stop it."""
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = botkit.make_broker(day)
    botkit.seed(config, broker, bars, day, {"S05": (10.0, 10.0)})
    carried = CarriedSell("swing", "S05", "time stop", f"{previous_day(bars, day):%Y-%m-%d}", 1)
    update_state(config, carried_sells={"S05": carried})
    close = bars.close.copy()
    close.loc[day, "S05"] = math.nan
    broken = Bars(bars.open, bars.high, bars.low, close, bars.volume)
    report = botkit.run(config, broker, broken, day)
    assert [(o.plan.symbol, o.carried) for o in report.orders if o.plan.side == "sell"] == [
        ("S05", True)
    ]
    assert [o.symbol for o in sent_sells(broker)] == ["S05"]


def test_every_position_stays_in_its_strategy_slice(
    tmp_path: Path, bars: Bars, day: pd.Timestamp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As in the backtest, a slice counts ALL its positions (money and slots), also those with
    a bad bar or a sell being sent again. Only the bad bar ones are not judged for exits."""
    seen: list[SleeveDay] = []
    real = planner.plan_sleeves

    def spy(sleeves: Sequence[SleeveDay], *args: Any, **kwargs: Any) -> AccountPlan:
        seen.extend(sleeves)
        return real(sleeves, *args, **kwargs)

    monkeypatch.setattr(runner, "plan_sleeves", spy)
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = botkit.make_broker(day)
    botkit.seed(config, broker, bars, day, dict.fromkeys(("S05", "S06", "S07"), (10.0, 10.0)))
    carried = CarriedSell("swing", "S06", "time stop", f"{previous_day(bars, day):%Y-%m-%d}", 1)
    update_state(config, carried_sells={"S06": carried})
    close = bars.close.copy()
    close.loc[day, "S05"] = math.nan
    broken = Bars(bars.open, bars.high, bars.low, close, bars.volume)
    report = botkit.run(config, broker, broken, day)
    (swing,) = seen
    assert set(swing.positions) == {"S05", "S06", "S07"}
    assert swing.frozen == {"S05"}
    sold = [(o.plan.symbol, o.carried) for o in report.orders if o.plan.side == "sell"]
    assert sorted(sold) == [("S06", True), ("S07", False)], "one sell each; S05 is not judged"


# ----------------------------------------------------------------------------- cash


@dataclass
class SellsAreRefused(FakeBroker):
    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        if request.side == "sell":
            raise BrokerError("Alpaca refused the order: insufficient qty available")
        return super().submit_order(request)


def test_buys_never_spend_money_from_sells(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    """Sell proceeds only arrive at the next open. Buys tonight may only use cash on hand."""
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    when = botkit.evening(day)
    broker = SellsAreRefused(
        cash=10_000.0,
        now=lambda: when,
        session=day.date(),
        balance_asof=(day - pd.offsets.BDay(1)).date(),
    )
    botkit.seed(config, broker, bars, day, {"S05": (275.0, 275.0)})
    cash = broker.cash
    report = botkit.run(config, broker, bars, day)
    spent = sum((o.notional or 0) for o in broker.orders.values() if o.side == "buy")
    assert spent <= cash - 0.02 * broker.get_account().equity + 1e-6
    assert report.exit_code is ExitCode.NEEDS_ATTENTION  # the refused sell needs a look


def test_cash_below_zero_stops_buys(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0}, ignore_symbols=("S01",))
    broker = botkit.make_broker(day, cash=-500.0)
    broker.positions["S01"] = BrokerPosition("S01", 30.0, 100.0, 3_000.0)  # yours, on margin
    save_state(
        BotState(account_id=botkit.FAKE_ACCOUNT, peak_equity=2_500.0), botkit.state_path(config)
    )
    report = botkit.run(config, broker, bars, day)
    assert not buys(report)
    assert "below zero" in report.entries_halted
    assert report.exit_code is ExitCode.NEEDS_ATTENTION


def test_an_account_that_can_borrow_is_warned(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    broker.multiplier = 2.0
    botkit.run(config, broker, bars, day)
    rows = pd.read_csv(config.bot.journal_file)
    warnings = rows[rows["event"] == "warning"]["detail"].tolist()
    assert any("no-margin" in str(w) for w in warnings)


# ----------------------------------------------------------------------------- data problems


def test_a_bad_bar_on_one_held_stock_gives_a_sells_only_run(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = botkit.make_broker(day)
    botkit.seed(config, broker, bars, day, {"S05": (10.0, 10.0), "S06": (10.0, 10.0)})
    close = bars.close.copy()
    close.loc[day, "S05"] = math.nan
    broken = Bars(bars.open, bars.high, bars.low, close, bars.volume)
    report = botkit.run(config, broker, broken, day)
    assert ("S06", 10.0) in sells(report), "the stock with a good bar still gets its exit"
    assert "S05" not in {s for s, _ in sells(report)}, "the stock with a bad bar is left alone"
    assert not buys(report)
    assert report.exit_code is ExitCode.NEEDS_ATTENTION
    assert "data_problem" in botkit.journal_events(config)


def test_many_bad_symbols_give_a_sells_only_run(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = botkit.make_broker(day)
    botkit.seed(config, broker, bars, day, {"S06": (10.0, 10.0)})
    close = bars.close.copy()
    close.loc[day, ["S01", "S02", "S03"]] = math.nan  # 3 of 12 stocks: over the 10% limit
    broken = Bars(bars.open, bars.high, bars.low, close, bars.volume)
    report = botkit.run(config, broker, broken, day)
    assert ("S06", 10.0) in sells(report)
    assert not buys(report)
    assert "no good bar" in report.entries_halted


# ----------------------------------------------------------------------------- money moving


def watching_movements(config: Any, bars: Bars, day: pd.Timestamp, peak: float) -> None:
    """Records that already watch deposits and withdrawals (as after the first run)."""
    state = BotState(
        account_id=botkit.FAKE_ACCOUNT,
        peak_equity=peak,
        movements_checked=f"{previous_day(bars, day):%Y-%m-%d}",
    )
    save_state(state, botkit.state_path(config))


def test_a_withdrawal_is_not_a_loss(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    """You take out 25%. Without the fix that trips the 20% drawdown STOP."""
    config = botkit.make_config(tmp_path)
    watching_movements(config, bars, day, peak=10_000.0)
    broker = botkit.make_broker(day, cash=7_500.0)
    broker.cash_movements = [CashMovement("w1", day.date(), -2_500.0, "CSW")]
    report = botkit.run(config, broker, bars, day)
    assert not config.bot.kill_switch_file.exists()
    assert report.orders and buys(report)
    state = load_state(botkit.state_path(config))
    assert state is not None and state.peak_equity == pytest.approx(7_500.0)
    assert "cash_movement" in botkit.journal_events(config)


def test_a_deposit_keeps_the_drawdown_the_same(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    watching_movements(config, bars, day, peak=11_000.0)  # 10,000 is 9.1% below it
    broker = botkit.make_broker(day, cash=15_000.0)  # after a 5,000 deposit
    broker.cash_movements = [CashMovement("d1", day.date(), 5_000.0, "CSD")]
    botkit.run(config, broker, bars, day)
    state = load_state(botkit.state_path(config))
    assert state is not None and state.peak_equity is not None
    assert state.peak_equity == pytest.approx(16_500.0)
    assert 15_000.0 / state.peak_equity - 1 == pytest.approx(10_000.0 / 11_000.0 - 1)


def test_each_movement_counts_once(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path)
    watching_movements(config, bars, day, peak=10_000.0)
    broker = botkit.make_broker(day, cash=7_500.0)
    broker.cash_movements = [CashMovement("w1", day.date(), -2_500.0, "CSW")]
    botkit.run(config, broker, bars, day, run_id="first")
    botkit.run(config, broker, bars, day, run_id="second")
    state = load_state(botkit.state_path(config))
    assert state is not None and state.peak_equity == pytest.approx(7_500.0)
    assert state.seen_movements == ["w1"]


def test_unreadable_deposits_and_withdrawals_stop_buys(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    broker.fail.add("get_cash_movements")
    report = botkit.run(config, broker, bars, day)
    assert not buys(report)
    assert "deposits and withdrawals" in report.entries_halted
    assert report.exit_code is ExitCode.NEEDS_ATTENTION


def test_moved_shares_ask_for_a_look(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path)
    watching_movements(config, bars, day, peak=10_000.0)
    broker = botkit.make_broker(day)
    broker.cash_movements = [CashMovement("j1", day.date(), 0.0, "JNLS", shares=True)]
    report = botkit.run(config, broker, bars, day)
    assert report.exit_code is ExitCode.NEEDS_ATTENTION
    assert any("reset-peak" in item for item in report.attention)


# ----------------------------------------------------------------------------- exit codes


def test_an_order_refused_at_the_open_needs_attention_next_run(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    botkit.run(config, broker, bars, day, run_id="first")
    for cid, order in list(broker.orders.items()):
        broker.orders[cid] = replace(order, status="rejected")
    after = bars.dates[list(bars.dates).index(day) + 1]
    botkit.move_to(broker, after)
    second = botkit.run(config, broker, bars, after, run_id="second")
    assert second.exit_code is ExitCode.NEEDS_ATTENTION
    assert any("rejected" in item for item in second.attention)


def test_a_position_you_did_not_list_needs_attention(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    save_state(
        BotState(account_id=botkit.FAKE_ACCOUNT, peak_equity=10_000.0), botkit.state_path(config)
    )
    broker = botkit.make_broker(day)
    broker.positions["S03"] = BrokerPosition("S03", 3.0, 50.0, 150.0)
    report = botkit.run(config, broker, bars, day)
    assert "S03" not in {o.plan.symbol for o in report.orders}
    assert "unmanaged" in botkit.journal_events(config)
    assert report.exit_code is ExitCode.NEEDS_ATTENTION
    assert "1 unmanaged" in report.status_line


def test_ignored_symbols_are_skipped_quietly(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path, ignore_symbols=("S03",))
    save_state(
        BotState(account_id=botkit.FAKE_ACCOUNT, peak_equity=10_000.0), botkit.state_path(config)
    )
    broker = botkit.make_broker(day)
    broker.positions["S03"] = BrokerPosition("S03", 3.0, 50.0, 150.0)
    report = botkit.run(config, broker, bars, day)
    assert "S03" not in {o.plan.symbol for o in report.orders}
    assert "unmanaged" not in botkit.journal_events(config)
    assert report.exit_code is ExitCode.OK
    assert "1 ignored, 0 unmanaged" in report.status_line


@pytest.mark.parametrize("name", ["STOP", "stop", "Stop.txt", "STOP.md"])
def test_the_kill_switch_works_in_any_case_and_with_an_extension(
    tmp_path: Path, bars: Bars, day: pd.Timestamp, name: str
) -> None:
    config = botkit.make_config(tmp_path)
    (tmp_path / name).write_text("stop", encoding="utf-8")
    report = botkit.run(config, botkit.make_broker(day), bars, day)
    assert not buys(report)
    assert f"kill switch ON ({name})" in report.status_line


def test_kill_switch_stops_buys_but_not_sells(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = botkit.make_broker(day)
    botkit.seed(config, broker, bars, day, {"S05": (10.0, 10.0)})
    config.bot.kill_switch_file.write_text("stop", encoding="utf-8")
    report = botkit.run(config, broker, bars, day)
    assert sells(report) == [("S05", 10.0)]
    assert not buys(report)
    assert "kill switch" in report.entries_halted


def test_daily_loss_limit_stops_buys(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    broker.last_equity = 10_000.0 / (1 - config.bot.daily_loss_limit_pct - 0.01)
    report = botkit.run(config, broker, bars, day)
    assert not buys(report)
    assert "fell" in report.entries_halted
    assert report.exit_code is ExitCode.NEEDS_ATTENTION
    assert any("fell" in item for item in report.attention)


@pytest.mark.parametrize("last", [0.0, -1.0, math.nan])
def test_a_missing_previous_close_stops_buys(
    tmp_path: Path, bars: Bars, day: pd.Timestamp, last: float
) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    broker.last_equity = last
    report = botkit.run(config, broker, bars, day)
    assert not buys(report)
    assert "previous close" in report.entries_halted
    assert report.exit_code is ExitCode.NEEDS_ATTENTION


def test_a_previous_close_from_the_wrong_day_stops_buys(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    broker.balance_asof = day.date()  # as in a morning run, after the broker rolled over
    report = botkit.run(config, broker, bars, day)
    assert not buys(report)
    assert "cannot be checked" in report.entries_halted
    assert report.exit_code is ExitCode.NEEDS_ATTENTION


def midnight(day: pd.Timestamp) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=NEW_YORK)


def test_the_portfolio_history_comes_first(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    """Alpaca's daily value for the previous trading day beats its last_equity number."""
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    broker.daily_equity = [(midnight(previous_day(bars, day)), 10_000.0 / 0.96)]  # a 4% fall
    report = botkit.run(config, broker, bars, day)
    assert not buys(report)
    assert "fell" in report.entries_halted and "portfolio history" in report.entries_halted
    assert report.exit_code is ExitCode.NEEDS_ATTENTION


def test_today_is_never_its_own_previous_close(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    broker.balance_asof = day.date()
    broker.daily_equity = [(midnight(day), 10_000.0)]  # today's own point: 0% by definition
    report = botkit.run(config, broker, bars, day)
    assert not buys(report)
    assert "cannot be checked" in report.entries_halted


def test_the_bots_own_saved_close_is_the_last_fallback(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    previous = previous_day(bars, day)
    state = BotState(account_id=botkit.FAKE_ACCOUNT, peak_equity=11_000.0)
    state.remember_close(f"{previous:%Y-%m-%d}", 10_000.0 / 0.96)
    save_state(state, botkit.state_path(config))
    broker = botkit.make_broker(day)
    broker.balance_asof = None  # Alpaca's number is no use tonight
    report = botkit.run(config, broker, bars, day)
    assert "fell" in report.entries_halted and "own run" in report.entries_halted
    saved = load_state(botkit.state_path(config))
    assert saved is not None and saved.close_equity[f"{day:%Y-%m-%d}"] == pytest.approx(10_000.0)


def test_daily_labels_belong_to_their_trading_day() -> None:
    """However Alpaca labels a day (New York midnight, UTC midnight, the previous close),
    the label maps to the same trading day."""
    thursday, friday, monday = date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 23)
    sessions = [thursday, friday, monday]
    labels = [
        datetime(2026, 11, 23, 0, 0, tzinfo=NEW_YORK),  # midnight in New York
        datetime(2026, 11, 23, 0, 0, tzinfo=UTC),  # midnight UTC = Sunday 7 PM in New York
        datetime(2026, 11, 20, 16, 0, tzinfo=NEW_YORK),  # the window starts at Friday's close
        datetime(2026, 11, 23, 9, 30, tzinfo=NEW_YORK),  # the open
    ]
    assert [session_of(label, sessions) for label in labels] == [monday] * 4
    assert session_of(datetime(2026, 11, 21, 12, 0, tzinfo=NEW_YORK), sessions) is None


def test_drawdown_limit_writes_stop_and_restarts_after_removal(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    path = botkit.state_path(config)
    peak = 10_000.0 / (1 - config.bot.max_drawdown_pct - 0.05)
    save_state(BotState(account_id=botkit.FAKE_ACCOUNT, peak_equity=peak), path)
    broker = botkit.make_broker(day)
    report = botkit.run(config, broker, bars, day)
    assert config.bot.kill_switch_file.exists()
    assert not buys(report)
    state = load_state(path)
    assert state is not None and state.drawdown_tripped
    config.bot.kill_switch_file.unlink()  # a human looked and cleared it
    restarted = botkit.run(config, broker, bars, day, run_id="after")
    state = load_state(path)
    assert state is not None and not state.drawdown_tripped
    assert state.peak_equity == pytest.approx(10_000.0)
    assert restarted.exit_code is ExitCode.OK


def test_a_partial_fill_without_a_time_uses_the_next_trading_day(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path)
    broker = botkit.make_broker(day)
    botkit.run(config, broker, bars, day, run_id="first")
    cid, order = next(iter(broker.orders.items()))
    broker.orders[cid] = replace(
        order, status="canceled", filled_qty=1.5, filled_avg_price=100.0, filled_at=None
    )
    broker.positions[order.symbol] = BrokerPosition(order.symbol, 1.5, 100.0, 150.0)
    later = bars.dates[list(bars.dates).index(day) + 3]
    botkit.move_to(broker, later)
    for other in list(broker.orders):
        if other != cid:
            broker.orders[other] = replace(broker.orders[other], status="canceled")
    botkit.run(config, broker, bars, later, run_id="second")
    state = load_state(botkit.state_path(config))
    assert state is not None
    next_day = bars.dates[list(bars.dates).index(day) + 1]
    assert state.positions[order.symbol].entry_date == f"{next_day:%Y-%m-%d}"


def test_a_sell_that_did_not_fill_is_sent_again(
    tmp_path: Path, bars: Bars, day: pd.Timestamp
) -> None:
    config = botkit.make_config(tmp_path, sleeves={"swing": 1.0})
    broker = botkit.make_broker(day)
    botkit.seed(config, broker, bars, day, {"S05": (10.0, 10.0)})
    first = botkit.run(config, broker, bars, day, run_id="first")
    assert sells(first) == [("S05", 10.0)]
    sell_id = next(o.client_order_id for o in first.orders if o.plan.side == "sell")
    broker.orders[sell_id] = replace(broker.orders[sell_id], status="expired")
    after = bars.dates[list(bars.dates).index(day) + 1]
    botkit.move_to(broker, after)
    second = botkit.run(config, broker, bars, after, run_id="second")
    again = [o for o in second.orders if o.plan.side == "sell" and o.plan.symbol == "S05"]
    assert again and again[0].carried
    assert again[0].client_order_id != sell_id
    assert second.exit_code is ExitCode.NEEDS_ATTENTION  # the expired sell is reported


def test_non_fractionable_stocks_are_skipped(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    probe = botkit.run(botkit.make_config(tmp_path / "p"), botkit.make_broker(day), bars, day)
    target = next(o.plan.symbol for o in probe.orders if o.plan.side == "buy")
    config = botkit.make_config(tmp_path / "again")
    broker = botkit.make_broker(day)
    broker.not_fractionable.add(target)
    report = botkit.run(config, broker, bars, day)
    assert target not in {o.symbol for o in report.results}
    assert report.exit_code is ExitCode.OK


def test_a_refused_order_asks_for_attention(tmp_path: Path, bars: Bars, day: pd.Timestamp) -> None:
    probe = botkit.run(botkit.make_config(tmp_path / "p"), botkit.make_broker(day), bars, day)
    target = probe.orders[0].plan.symbol
    config = botkit.make_config(tmp_path / "real")
    broker = botkit.make_broker(day)
    broker.reject.add(target)
    report = botkit.run(config, broker, bars, day)
    assert report.exit_code is ExitCode.NEEDS_ATTENTION
    assert any(o.status == "rejected" for o in report.results)
