"""One hands off bot run, from reading the account to logging the last order.

The steps, in order (see the diagram in docs/HOW_WE_TRADE.md):

1. Read the account, positions, open orders and the market clock.
2. Open the records for THIS account and mode. Missing records are only a
   "first run" when the account is empty.
3. Read back last run's orders: did they fill? An "accepted" order is not a
   fill. Only the broker's filled quantity counts. A sell that did not go
   through is remembered and sent again, with a new order id.
4. Reconcile the records with the broker. The bot never sells more than its
   records say it bought; shares you added by hand are never touched. A
   position that is not in the records is never taken over: it needs a look.
5. Deposits and withdrawals: the best level moves with them, so moving money
   never counts as a gain or a loss.
6. Safety switches: the STOP file, cash below zero, the drawdown limit.
7. Download fresh bars and check them. A problem with a few symbols means a
   sells only run; a problem with the data source as a whole fails closed.
8. The daily loss limit, against a previous close the bot can trust.
9. Ask each strategy for exits and entries through ``planner.plan_sleeves``,
   the same function the backtest uses.
10. Drop anything already sent, run the last checks, look at the clock again.
11. Send sells, then buys, as market DAY orders for the next open. Each order
    is written to the records BEFORE it is sent.
12. Read back every order, read positions again, save the records.

Fail closed: if anything we need cannot be read, or looks wrong, the bot
sends NO orders and exits with code 2. Doing nothing is safe for a day. It is
not safe for long, because nothing rests at the broker: after a second failed
run in a row the bot warns loudly that open positions have no protection.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import IntEnum, StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from hsw_bot.broker.base import (
    AccountSnapshot,
    Broker,
    BrokerOrder,
    BrokerPosition,
    CashMovement,
    DuplicateOrderError,
    MarketClock,
    OrderRequest,
    mask,
)
from hsw_bot.config import STATE_FILE_NAME, Config, ConfigError
from hsw_bot.data import Bars, check_latest
from hsw_bot.journal import Journal
from hsw_bot.planner import OrderPlan, SleeveDay, build_table, plan_sleeves
from hsw_bot.rebuild import make_order_id, market_day
from hsw_bot.state import (
    SENT,
    SUBMITTING,
    BotState,
    CarriedSell,
    PendingOrder,
    PositionRecord,
    checked_peak,
    load_state,
    save_state,
)
from hsw_bot.strategies import Strategy, build_strategy
from hsw_bot.strategies.base import Day, Position

log = logging.getLogger("hsw_bot")

MARKET_TZ = ZoneInfo("America/New_York")
SECONDS_PER_MINUTE = 60
QTY_TOLERANCE = 1e-6  # share counts closer than this are "the same"
SIZE_TOLERANCE = 1.01  # a buy may be 1% over its cap before the bug guard trips
UNSETTLED = frozenset({"pending_new"})  # the broker has not answered yet
NOT_FILLED = frozenset({"canceled", "expired", "rejected", "done_for_day"})
KILL_SWITCH_NAME = "stop"
MARKET_CLOSE = time(16, 0)  # New York time
HISTORY_LOOKBACK_DAYS = 10  # how far back to ask for daily account values
MOVEMENT_LOOKBACK_DAYS = 7  # deposits can be dated a few days before they show up

DataLoader = Callable[[Sequence[str], datetime], Bars]


class Mode(StrEnum):
    DRY_RUN = "dry-run"
    PAPER = "paper"
    LIVE = "live"


class ExitCode(IntEnum):
    OK = 0
    NEEDS_ATTENTION = 1  # the run finished, but something needs a human look; read the log
    FAILED_CLOSED = 2  # a safety check failed; nothing was sent
    SETUP_ERROR = 3  # settings or keys are wrong


class FailClosedError(Exception):
    """A safety check failed. The bot sends no (more) orders."""


@dataclass(frozen=True)
class PlannedOrder:
    sleeve: str
    plan: OrderPlan
    client_order_id: str
    price: float  # today's close, to show an estimate
    carried: bool = False


@dataclass
class Progress:
    """What has happened so far in this run, so a late failure is reported truthfully."""

    sent: int = 0  # orders the broker has (or may have) received this run
    state: BotState | None = None


@dataclass
class Reconciled:
    """What the account holds, sorted by whose it is."""

    managed: set[str] = field(default_factory=set)  # the bot's own positions
    extra: dict[str, float] = field(default_factory=dict)  # shares beyond the bot's, per symbol
    unmanaged: set[str] = field(default_factory=set)  # not the bot's and not on the ignore list
    ignored: set[str] = field(default_factory=set)  # yours, by bot.ignore_symbols or rebuild-state
    others_value: float = 0.0  # what all the shares that are not the bot's are worth


@dataclass
class RunReport:
    mode: Mode
    exit_code: ExitCode
    orders: list[PlannedOrder] = field(default_factory=list)
    results: list[BrokerOrder] = field(default_factory=list)
    entries_halted: str = ""
    market_date: pd.Timestamp | None = None
    error: str = ""
    status_line: str = ""
    attention: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sent: int = 0


@dataclass
class BotContext:
    """Everything one run needs. Tests swap in a fake broker, fake data and a fake clock."""

    config: Config
    broker: Broker
    mode: Mode
    load_bars: DataLoader
    journal: Journal
    state_path: Path
    sleep: Callable[[float], None]


def run_once(ctx: BotContext) -> RunReport:
    """Do one full run. Trading problems become an exit code, never a crash."""
    progress = Progress()
    try:
        return _run(ctx, progress)
    except FailClosedError as err:
        return _stopped(ctx, progress, str(err))
    except ConfigError:
        raise
    except Exception as err:  # a bug or a disk error: stop, and say what was already sent
        log.exception("Unexpected error during the run.")
        return _stopped(ctx, progress, f"Unexpected error: {err}.")


def _stopped(ctx: BotContext, progress: Progress, why: str) -> RunReport:
    """End a run that could not finish, and tell the truth about what was sent."""
    if progress.sent:
        message = (
            f"{why} {progress.sent} order(s) WERE sent. Check the broker (hsw-bot status, or "
            "the Alpaca dashboard) before doing anything by hand."
        )
        _quiet_journal(ctx.journal, "stopped_after_sending", message)
        return RunReport(ctx.mode, ExitCode.NEEDS_ATTENTION, error=message, sent=progress.sent)
    message = f"{why} No orders were sent."
    _quiet_journal(ctx.journal, "fail_closed", message)
    streak = _count_failure(ctx, progress)
    warnings = []
    if streak >= ctx.config.bot.warn_after_failed_runs:
        warnings.append(
            f"WARNING: the bot has failed closed {streak} runs in a row. Your open positions "
            "have NO stop protection: nothing rests at the broker. Fix the cause above, or "
            "manage the positions by hand in the Alpaca dashboard."
        )
        _quiet_journal(ctx.journal, "fail_closed", warnings[0])
    return RunReport(ctx.mode, ExitCode.FAILED_CLOSED, error=message, warnings=warnings)


def _count_failure(ctx: BotContext, progress: Progress) -> int:
    """Add one to the failed run streak in the records, if the records can be read."""
    state = progress.state
    try:
        if state is None:
            state = load_state(ctx.state_path)
        if state is None:
            return 0
        state.fail_streak += 1
        save_state(state, ctx.state_path)
    except Exception:  # the records themselves may be the problem
        return 0
    return state.fail_streak


def _quiet_journal(journal: Journal, event: str, detail: str) -> None:
    try:
        journal.write(event, detail)
    except Exception:  # a full disk must not hide the real message
        log.error("%s: %s", event, detail)


def _run(ctx: BotContext, progress: Progress) -> RunReport:
    cfg, broker, journal = ctx.config, ctx.broker, ctx.journal
    bot = cfg.bot
    journal.write("run_start", f"broker: {broker.label}")
    strategies = {name: build_strategy(cfg, name) for name in bot.sleeves}
    check_sleeves(strategies)
    check_old_records(cfg, ctx.state_path, ctx.mode)

    loaded = _read("the bot's records", lambda: load_state(ctx.state_path))
    account = _read("the account", broker.get_account)
    check_account(account)
    held = {p.symbol: p for p in _read("positions", broker.get_positions)}
    check_positions(held.values())
    open_orders = _read("open orders", broker.get_open_orders)
    clock = _read("the market clock", broker.get_clock)
    check_timing(clock, ctx.mode, bot.minutes_before_open_cutoff, journal)

    ignored = set(bot.ignore_symbols)
    state = open_records(loaded, account, held, open_orders, ignored, ctx.mode, journal)
    progress.state = state
    ignored |= set(state.own_symbols)
    check_record_sleeves(state, bot.sleeves)

    attention: list[str] = []
    resolve_pending(state, broker, journal, attention)
    in_flight = {p.symbol for p in state.pending.values()} | {o.symbol for o in open_orders}
    recon = reconcile(state, held, in_flight, ignored, journal, attention)
    flag_unmanaged(recon, journal, attention)
    today = clock.now.astimezone(MARKET_TZ).date()
    moved_why = apply_cash_movements(state, broker, account, today, journal, attention)
    allow_entries, halted = safety_switches(state, account, cfg, ctx.mode, journal, attention)
    status = status_line(ctx.mode, account, cfg, recon, state)
    journal.write("status", status)

    wanted = sorted({s for st in strategies.values() for s in st.symbols()} | set(state.positions))
    bars = _read("price data", lambda: ctx.load_bars(wanted, clock.now))
    bars, market_date = settle_market_date(bars, broker, clock, ctx.mode, cfg)
    check_history(bars, strategies)
    settle_entry_dates(state, bars)
    bad, data_why = check_data(
        bars, wanted, market_date, state, bot.max_bad_data_pct, journal, attention
    )
    loss_why = daily_loss_check(account, broker, state, bars, market_date, cfg, ctx.mode, journal)
    if loss_why:
        attention.append(f"no new buys: {loss_why}")
    reasons = [r for r in (halted, moved_why, data_why, loss_why) if r]
    allow_entries = allow_entries and not (moved_why or data_why or loss_why)
    if reasons:
        journal.write("safety", f"no new buys this run: {'; '.join(reasons)}")

    blocked = set(bad) | recon.unmanaged | set(recon.extra) | ignored | in_flight
    orders = plan_all(
        ctx,
        strategies,
        state,
        account,
        held,
        recon,
        bars,
        blocked,
        in_flight,
        set(bad),
        allow_entries,
    )
    orders = drop_already_sent(orders, state, journal)
    orders = check_assets(orders, broker, journal)
    guard_orders(orders, account, held, state, recon, cfg)
    if orders:  # the clock again, right before money moves
        fresh = _read("the market clock", broker.get_clock)
        check_timing(fresh, ctx.mode, bot.minutes_before_open_cutoff, journal)

    results, unclear, refused = submit_all(orders, broker, state, ctx, journal, progress, attention)
    results = read_back(results, broker, ctx, journal)
    after = _read("positions after sending", broker.get_positions)
    journal.write("positions", f"{len(after)} positions at the broker after this run")

    warnings = failed_sell_warnings(state, bot.warn_after_failed_sells, journal, attention)
    if ctx.mode is not Mode.DRY_RUN:
        state.remember_close(f"{market_date:%Y-%m-%d}", account.equity)
    state.fail_streak = 0
    state.last_run_utc = datetime.now(UTC).isoformat(timespec="seconds")
    save_state(state, ctx.state_path)
    problems = [o for o in results if o.status in NOT_FILLED]
    attention += [f"order for {o.symbol} came back {o.status}" for o in problems]
    if unclear:
        attention.append(f"{unclear} order(s) may or may not have reached the broker")
    if refused:
        attention.append(f"{refused} order(s) could not be sent")
    code = ExitCode.NEEDS_ATTENTION if attention else ExitCode.OK
    journal.write("run_end", f"{len(results)} orders sent, exit code {int(code)}")
    return RunReport(
        mode=ctx.mode,
        exit_code=code,
        orders=orders,
        results=results,
        entries_halted="; ".join(reasons),
        market_date=market_date,
        status_line=status,
        attention=attention,
        warnings=warnings,
        sent=progress.sent,
    )


# ----------------------------------------------------------------------------- reads and checks


def _read[T](what: str, action: Callable[[], T]) -> T:
    """Read something we cannot trade without. ANY failure fails closed."""
    try:
        return action()
    except FailClosedError:
        raise
    except Exception as err:
        raise FailClosedError(f"Could not read {what}: {err}") from err


def check_sleeves(strategies: Mapping[str, Strategy]) -> None:
    """Two strategies must never trade the same symbol in one account.

    The broker keeps one position per symbol. If two strategies shared one,
    neither could tell which shares were its own.
    """
    owner: dict[str, str] = {}
    for name, strategy in strategies.items():
        for symbol in strategy.tradable():
            if symbol in owner:
                raise ConfigError(
                    f"{symbol} can be bought by both {owner[symbol]} and {name}. "
                    "Give each strategy in [bot] sleeves its own symbols."
                )
            owner[symbol] = name


def check_account(account: AccountSnapshot) -> None:
    numbers = (account.equity, account.cash)
    if not all(math.isfinite(n) for n in numbers):
        raise FailClosedError("The account numbers are not valid numbers.")
    if account.equity <= 0:
        raise FailClosedError(f"Account equity is {account.equity:.2f}. Nothing to trade with.")
    if account.blocked:
        raise FailClosedError(f"The broker has blocked trading on this account ({account.status}).")
    if not account.account_id:
        raise FailClosedError("The broker did not say which account this is.")


def check_positions(positions: Iterable[BrokerPosition]) -> None:
    for p in positions:
        if not (math.isfinite(p.qty) and math.isfinite(p.market_value)):
            raise FailClosedError(f"The position in {p.symbol} has invalid numbers.")
        if p.qty < 0:
            raise FailClosedError(
                f"The account is short {p.symbol}. This bot never shorts; check by hand."
            )


def check_timing(clock: MarketClock, mode: Mode, cutoff_minutes: int, journal: Journal) -> None:
    """Only trade while the market is closed, so orders fill at the next open like the backtest."""
    if clock.is_open:
        message = (
            "The market is open right now. Run the bot after the close (6 PM Eastern or later), "
            "so orders fill at the next open, as in the backtest."
        )
        if mode is not Mode.DRY_RUN:
            raise FailClosedError(message)
        journal.write("note", "market is open; the dry run uses the last finished trading day")
        return
    minutes = (clock.next_open - clock.now).total_seconds() / SECONDS_PER_MINUTE
    if minutes < cutoff_minutes:
        message = (
            f"The market opens in {minutes:.0f} minutes. That is too close; run it after the close."
        )
        if mode is not Mode.DRY_RUN:
            raise FailClosedError(message)
        journal.write("note", message)


def settle_market_date(
    bars: Bars, broker: Broker, clock: MarketClock, mode: Mode, cfg: Config
) -> tuple[Bars, pd.Timestamp]:
    """Find the last finished trading day and make sure the bars reach it, and no further.

    With Alpaca we ask its calendar which session closed last. The newest bar
    must be from exactly that day: older means Yahoo has not updated yet
    (stale, so the whole run fails closed); a newer bar would be a half
    finished day and is dropped. The fake broker has no calendar, so a dry
    run instead checks that the newest bar is at most ``stale_data_days`` old.
    """
    session = _read("the market calendar", lambda: broker.last_session(clock.now))
    today = pd.Timestamp(clock.now.astimezone(MARKET_TZ).date())
    if session is not None:
        expected = pd.Timestamp(session)
        bars = bars.drop_dates([d for d in bars.dates if d > expected])
        if len(bars.dates) == 0 or bars.dates[-1] < expected:
            newest = bars.dates[-1].date() if len(bars.dates) else "none"
            raise FailClosedError(
                f"The newest price bar is from {newest}, but the last trading day was "
                f"{expected.date()}. Yahoo may not have updated yet; try again in an hour."
            )
        return bars, expected
    if mode is not Mode.DRY_RUN:
        raise FailClosedError("The broker could not tell which trading day closed last.")
    closes_today = clock.next_close.astimezone(MARKET_TZ).date() == today.date()
    if clock.is_open or closes_today:
        bars = bars.drop_dates([d for d in bars.dates if d >= today])  # today is not finished
    if len(bars.dates) == 0:
        raise FailClosedError("No price bars came back.")
    newest = pd.Timestamp(bars.dates[-1])
    if (today - newest).days > cfg.bot.stale_data_days:
        raise FailClosedError(f"The newest price bar is from {newest.date()}. That looks stale.")
    return bars, newest


def check_history(bars: Bars, strategies: Mapping[str, Strategy]) -> None:
    """Make sure we downloaded enough days for the longest average."""
    for name, strategy in strategies.items():
        need = strategy.warmup_days()
        if len(bars.dates) < need:
            raise FailClosedError(
                f"{name} needs {need} trading days of history, but only {len(bars.dates)} came "
                "back. Raise bot.history_days in config.toml."
            )


def check_data(
    bars: Bars,
    symbols: Sequence[str],
    market_date: pd.Timestamp,
    state: BotState,
    limit: float,
    journal: Journal,
    attention: list[str],
) -> tuple[frozenset[str], str]:
    """Find symbols with a bad last bar. Returns them, and why buys must stop (or "").

    A problem limited to some symbols is not a reason to stop everything:
    held stocks with good bars still get their exits checked. The held stocks
    with a bad bar are left alone this run and flagged, and no new buys go
    out. Only when no symbol at all has a good bar does the run fail closed.
    """
    result = check_latest(bars, symbols, market_date)
    if not result.good:
        raise FailClosedError(
            f"No symbol has a good bar for {market_date.date()}. The data source looks broken."
        )
    reasons = []
    held_bad = sorted(result.bad & set(state.positions))
    for symbol in held_bad:
        journal.write(
            "data_problem",
            "no good bar today for a stock we hold; left alone this run",
            symbol=symbol,
        )
    if held_bad:
        attention.append(f"no good price today for held {', '.join(held_bad)}")
        reasons.append(f"no good price for held {', '.join(held_bad)} (sells only run)")
    for symbol in sorted(result.bad - set(held_bad)):
        journal.write("skip_symbol", "no good bar for the last trading day", symbol=symbol)
    if len(result.bad) > limit * max(len(symbols), 1):
        attention.append(f"{len(result.bad)} of {len(symbols)} symbols have no good bar")
        reasons.append(
            f"{len(result.bad)} of {len(symbols)} symbols have no good bar (sells only run)"
        )
    return result.bad, "; ".join(reasons)


# ----------------------------------------------------------------------------- the records


def check_old_records(cfg: Config, state_path: Path, mode: Mode) -> None:
    """Refuse to run beside a records file left by an older version of the bot.

    Older versions kept ONE file, state/bot_state.json, for paper and live
    alike, and it does not say which account it belongs to. Starting fresh
    beside it would quietly drop its positions and its best level.
    """
    if mode is Mode.DRY_RUN:
        return
    old = cfg.bot.state_dir / STATE_FILE_NAME
    if not old.is_file() or old.resolve() == state_path.resolve():
        return
    live = " --live" if mode is Mode.LIVE else ""
    peak = _old_peak(old)
    choice = f"--peak {peak:.2f} (the best level in the old file)" if peak else "--peak-now"
    raise FailClosedError(
        f"An older version of the bot left its records in {old}. That file does not say "
        "which account it belongs to, so the bot will not guess. Look at it and check which "
        f"account it is from. Then move it out of the {cfg.bot.state_dir.name} folder (for "
        "example rename it to old_bot_state.json.txt), and rebuild this account's records: "
        f"hsw-bot rebuild-state{live} {choice}"
    )


def _old_peak(path: Path) -> float | None:
    """The best level in an old records file, if it can be read."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return checked_peak(raw.get("peak_equity"))
    except Exception:  # only a hint for the message
        return None


def open_records(
    loaded: BotState | None,
    account: AccountSnapshot,
    held: Mapping[str, BrokerPosition],
    open_orders: Sequence[BrokerOrder],
    ignored: set[str],
    mode: Mode,
    journal: Journal,
) -> BotState:
    """The records for THIS account, or a clear refusal.

    * No file and an empty account: a normal first run.
    * No file, but the account holds positions or open orders: refuse. The
      records were lost, and without them the stops and time exits of the
      bot's positions would silently stop working.
    * A file from another account: refuse.
    """
    holdings = sorted(s for s in held if s not in ignored)
    waiting = [o for o in open_orders if o.symbol not in ignored]
    live = " --live" if mode is Mode.LIVE else ""
    if loaded is None:
        if holdings or waiting:
            raise FailClosedError(
                f"The bot's records are missing, but the account holds {len(holdings)} "
                f"position(s) and {len(waiting)} open order(s). Rebuild the records from "
                f"Alpaca's order history with: hsw-bot rebuild-state{live} --peak-now (or, if "
                f"every position is yours: hsw-bot rebuild-state{live} --fresh-start --peak-now)."
            )
        journal.write("first_run", "no records yet and the account is empty: starting fresh")
        return BotState(account_id=account.account_id, peak_equity=account.equity)
    if loaded.account_id != account.account_id:
        raise FailClosedError(
            f"These records belong to account {mask(loaded.account_id or '')}, but the keys "
            f"open account {account.masked_number}. Each account and mode keeps its own records "
            "(state/paper/ and state/live/). Check the keys in .env."
        )
    if loaded.peak_equity is None:
        raise FailClosedError(
            "The records have no best account level, so the drawdown limit cannot work. "
            f"Run: hsw-bot reset-peak{live}"
        )
    return loaded


def check_record_sleeves(state: BotState, sleeves: Mapping[str, float]) -> None:
    """Refuse to run if the records hold positions for a strategy that is not configured."""
    orphans = sorted({r.sleeve for r in state.positions.values()} - set(sleeves))
    if orphans:
        name = orphans[0]
        raise FailClosedError(
            f"The records hold positions for {', '.join(orphans)}, which is not in [bot] sleeves "
            f"in config.toml. Without it those positions would never get their exits. To wind "
            f"it down, add {name} = 0.0 to sleeves: it then only sells, on its normal rules."
        )


def resolve_pending(
    state: BotState, broker: Broker, journal: Journal, attention: list[str]
) -> None:
    """Find out what happened to the orders we sent (or tried to send) last time.

    An order the broker "accepted" is only a promise to try. Here we look at
    the broker's filled quantity and average price, and only then update our
    records. This is the ONLY way a position enters the records.

    A sell that did not go through (refused, canceled, expired, or it never
    reached the broker) is carried: the next run sends it again, with a new
    order id, without judging it again.
    """
    for client_id, pending in list(state.pending.items()):
        order = _read_order(broker, client_id)
        if order is None:
            del state.pending[client_id]
            never = pending.status == SUBMITTING
            why = "it never reached the broker" if never else "the broker does not know the order"
            if pending.side == "sell" and carry_sell(
                state,
                pending.sleeve,
                pending.symbol,
                pending.reason,
                pending.signal_date,
                why,
                journal,
                attention,
            ):
                continue
            if never:
                journal.write(
                    "note",
                    f"order {client_id} never reached the broker; nothing was bought",
                    symbol=pending.symbol,
                    sleeve=pending.sleeve,
                )
            else:
                journal.write(
                    "mismatch",
                    f"order {client_id} is unknown at the broker; dropping it",
                    symbol=pending.symbol,
                    sleeve=pending.sleeve,
                )
                attention.append(f"order {client_id} is unknown at the broker")
            continue
        if order.is_open:
            pending.status = SENT
            journal.write(
                "waiting",
                "order still waiting at the broker",
                symbol=order.symbol,
                sleeve=pending.sleeve,
                status=order.status,
            )
            continue
        if order.filled_qty > 0:
            _apply_fill(state, pending, order, journal)
        if not order.is_filled:
            journal.write(
                "order_problem",
                f"order ended as {order.status}",
                symbol=order.symbol,
                sleeve=pending.sleeve,
                side=order.side,
                status=order.status,
            )
            why = f"it ended as {order.status}"
            carried = order.side == "sell" and carry_sell(
                state,
                pending.sleeve,
                order.symbol,
                pending.reason,
                pending.signal_date,
                why,
                journal,
                attention,
            )
            if not carried:
                attention.append(f"{order.side} order for {order.symbol} ended as {order.status}")
        del state.pending[client_id]


def carry_sell(
    state: BotState,
    sleeve: str,
    symbol: str,
    reason: str,
    since: str,
    why: str,
    journal: Journal,
    attention: list[str],
) -> bool:
    """Remember a sell that did not go through, so the next run sends it again.

    Each failure adds one to its count of tries. The next try gets a new order
    id (``...-sell-r2``), because a broker may refuse an id it has seen before.
    Returns False if there is nothing left to sell.
    """
    if symbol not in state.positions:
        return False
    old = state.carried_sells.get(symbol)
    tries = (old.tries if old else 0) + 1
    first_reason = old.reason if old else (reason or "the planned exit")
    state.carried_sells[symbol] = CarriedSell(
        sleeve, symbol, first_reason, old.since if old else since, tries
    )
    journal.write(
        "carried_sell",
        f"the sell did not go through ({why}); the next run sends it again "
        f"(failed {tries} time(s) so far)",
        sleeve=sleeve,
        symbol=symbol,
        side="sell",
    )
    attention.append(
        f"the sell of {symbol} did not go through ({why}); the next run sends it again"
    )
    return True


def failed_sell_warnings(
    state: BotState, limit: int, journal: Journal, attention: list[str]
) -> list[str]:
    """Loud warnings for sells that keep failing: the position has no working exit."""
    found = []
    for symbol, carried in sorted(state.carried_sells.items()):
        if carried.tries < limit:
            continue
        message = (
            f"WARNING: the sell of {symbol} has failed {carried.tries} times. Until it goes "
            "through, that position has NO working exit. If the next try fails too, sell it "
            "by hand in the Alpaca dashboard; the bot's records follow the broker."
        )
        journal.write("warning", message, sleeve=carried.sleeve, symbol=symbol)
        attention.append(f"the sell of {symbol} has failed {carried.tries} times")
        found.append(message)
    return found


def _read_order(broker: Broker, client_id: str) -> BrokerOrder | None:
    try:
        return broker.get_order(client_id)
    except Exception as err:
        raise FailClosedError(f"Could not read back order {client_id}: {err}") from err


def _apply_fill(
    state: BotState, pending: PendingOrder, order: BrokerOrder, journal: Journal
) -> None:
    price = order.filled_avg_price or 0.0
    fill_day = market_day(order.filled_at)  # "" if unknown: set from the bars later
    record = state.positions.get(order.symbol)
    if order.side == "buy":
        if record is None:
            state.positions[order.symbol] = PositionRecord(
                pending.sleeve, order.symbol, order.filled_qty, price, fill_day, pending.signal_date
            )
        else:
            total = record.qty + order.filled_qty
            record.entry_price = (
                record.qty * record.entry_price + order.filled_qty * price
            ) / total
            record.qty = total
    elif record is not None:
        record.qty -= order.filled_qty
        if record.qty <= QTY_TOLERANCE:
            del state.positions[order.symbol]
            state.carried_sells.pop(order.symbol, None)
    journal.write(
        "fill",
        f"filled on {fill_day or 'an unknown day'}",
        sleeve=pending.sleeve,
        symbol=order.symbol,
        side=order.side,
        qty=order.filled_qty,
        price=price,
        status=order.status,
    )


def settle_entry_dates(state: BotState, bars: Bars) -> None:
    """A fill without a time gets the first trading day after its signal as its entry day.

    That is the earliest it can have filled, so the time stop is never late.
    """
    for record in state.positions.values():
        if record.entry_date:
            continue
        later = bars.dates[bars.dates > pd.Timestamp(record.signal_date)]
        record.entry_date = f"{later[0]:%Y-%m-%d}" if len(later) else record.signal_date


def reconcile(
    state: BotState,
    held: Mapping[str, BrokerPosition],
    in_flight: set[str],
    ignored: set[str],
    journal: Journal,
    attention: list[str],
) -> Reconciled:
    """Compare the records with the broker. The bot only ever trades its own shares.

    * The broker holds FEWER shares than the records: the records shrink to the
      broker's number (someone sold some by hand).
    * The broker holds MORE: the extra shares are yours. The bot will only ever
      sell its own shares, and it buys no more of that stock.
    * A position with no record is not the bot's ("unmanaged"), unless it is
      on your ignore list. The bot never touches it.
    """
    result = Reconciled()
    for symbol, record in list(state.positions.items()):
        if symbol in in_flight:
            result.managed.add(symbol)
            continue  # an order is still working; judge it next run
        position = held.get(symbol)
        if position is None:
            journal.write(
                "mismatch",
                "the records say the bot holds it, the broker says it does "
                "not; dropping the record",
                sleeve=record.sleeve,
                symbol=symbol,
            )
            attention.append(f"the bot's {symbol} position is gone at the broker")
            del state.positions[symbol]
            state.carried_sells.pop(symbol, None)
            continue
        result.managed.add(symbol)
        if position.qty < record.qty - QTY_TOLERANCE * max(1.0, record.qty):
            journal.write(
                "mismatch",
                f"records say {record.qty:.6f} shares, the broker holds "
                f"{position.qty:.6f}; using the broker's smaller number",
                sleeve=record.sleeve,
                symbol=symbol,
            )
            attention.append(f"{symbol}: fewer shares at the broker than in the records")
            record.qty = position.qty
        elif position.qty > record.qty + QTY_TOLERANCE * max(1.0, record.qty):
            extra = position.qty - record.qty
            result.extra[symbol] = extra
            result.others_value += extra * _unit_value(position)
            if symbol not in ignored:
                journal.write(
                    "mismatch",
                    f"the broker holds {extra:.6f} more shares than the bot "
                    f"bought. The bot will only ever sell its own {record.qty:.6f}, and "
                    "buys no more of it",
                    sleeve=record.sleeve,
                    symbol=symbol,
                )
                attention.append(f"{symbol}: {extra:.4f} extra shares the bot did not buy")
    for symbol, position in held.items():
        if symbol in state.positions or symbol in in_flight:
            continue
        if symbol in ignored:
            result.ignored.add(symbol)
        else:
            result.unmanaged.add(symbol)
        result.others_value += position.market_value
    for symbol in [s for s in state.carried_sells if s not in state.positions]:
        del state.carried_sells[symbol]  # nothing left to sell
    return result


def flag_unmanaged(recon: Reconciled, journal: Journal, attention: list[str]) -> None:
    for symbol in sorted(recon.unmanaged):
        journal.write(
            "unmanaged",
            "this position was not opened by the bot; it will not touch it. "
            "If it is yours, add it to bot.ignore_symbols",
            symbol=symbol,
        )
    if recon.unmanaged:
        attention.append(f"positions the bot did not open: {', '.join(sorted(recon.unmanaged))}")


def _unit_value(position: BrokerPosition) -> float:
    return position.market_value / position.qty if position.qty > 0 else 0.0


# ----------------------------------------------------------------------------- money moving


def apply_cash_movements(
    state: BotState,
    broker: Broker,
    account: AccountSnapshot,
    today: date,
    journal: Journal,
    attention: list[str],
) -> str:
    """Keep the drawdown limit blind to money moving in or out of the account.

    The drawdown limit compares the account with its best level. A withdrawal
    is not a loss, and a deposit is not a gain. So for each new deposit or
    withdrawal, the best level moves by the same factor as the account:

        new best level = old best level x (equity after / equity before)

    The drawdown percentage then stays exactly what it was. Each movement is
    counted once (by its id). Returns why new buys must stop, or "": if the
    movements cannot be read, the bot cannot tell a withdrawal from a loss.
    """
    checked = state.movements_checked
    start = date.fromisoformat(checked) if checked else today
    try:
        movements = broker.get_cash_movements(start - timedelta(days=MOVEMENT_LOOKBACK_DAYS))
    except Exception as err:
        journal.write("safety", f"could not read deposits and withdrawals: {err}")
        attention.append("could not read the deposits and withdrawals, so no new buys")
        return "the deposits and withdrawals could not be read"
    seen = set(state.seen_movements)
    new = [m for m in movements if m.movement_id not in seen]
    if checked is None:  # new records: the best level was just set, after all of these
        journal.write("note", "watching deposits and withdrawals from today on")
    else:
        _move_peak(state, account, [m for m in new if not m.shares], journal, attention)
        for m in new:
            if m.shares:
                journal.write(
                    "cash_movement",
                    f"shares moved in or out ({m.kind} on {m.day}); the best level was not changed",
                )
                attention.append(
                    f"shares moved in or out of the account ({m.kind} on {m.day}). If that "
                    "changed the account value a lot, set a new best level: hsw-bot reset-peak"
                )
    state.remember_movements([m.movement_id for m in new])
    state.movements_checked = today.isoformat()
    return ""


def _move_peak(
    state: BotState,
    account: AccountSnapshot,
    cash: Sequence[CashMovement],
    journal: Journal,
    attention: list[str],
) -> None:
    if not cash or state.peak_equity is None:
        return
    for m in cash:
        kind = "deposit" if m.amount > 0 else "withdrawal"
        journal.write("cash_movement", f"{kind} of ${abs(m.amount):,.2f} ({m.kind} on {m.day})")
    before = account.equity - math.fsum(m.amount for m in cash)
    old = state.peak_equity
    if before <= 0:
        state.peak_equity = account.equity
        message = "the account was empty before this deposit; the best level starts again today"
        attention.append(message)
    else:
        state.peak_equity = old * account.equity / before
        message = (
            f"the best level moves from ${old:,.2f} to ${state.peak_equity:,.2f}, so money "
            "moving in or out does not change the drawdown"
        )
    journal.write("cash_movement", message)


# ----------------------------------------------------------------------------- safety switches


def find_kill_switch(folder: Path) -> Path | None:
    """Any file called "stop" in the bot's folder, in any case, with any extension.

    On Windows a new text file named STOP is really "STOP.txt", and on Linux
    "stop" and "STOP" are different files. Both must work.
    """
    if not folder.is_dir():
        return None
    for entry in sorted(folder.iterdir()):
        if entry.is_file() and entry.name.split(".")[0].lower() == KILL_SWITCH_NAME:
            return entry
    return None


def safety_switches(
    state: BotState,
    account: AccountSnapshot,
    cfg: Config,
    mode: Mode,
    journal: Journal,
    attention: list[str],
) -> tuple[bool, str]:
    """Decide whether new BUYS are allowed this run. Sells are always allowed.

    * The kill switch: a STOP file means no new buys.
    * Cash below zero: the account is borrowing. No new buys.
    * The drawdown limit: if the account is too far below its best level, the
      bot writes the STOP file itself. A human must look, then delete it.
      The best level then starts again from that day.
    """
    bot = cfg.bot
    folder = bot.kill_switch_file.parent
    if state.drawdown_tripped and find_kill_switch(folder) is None:
        journal.write("safety", "STOP was removed after a drawdown stop; restarting from today")
        state.drawdown_tripped = False
        state.peak_equity = account.equity
    peak = _valid_peak(state.peak_equity)
    state.peak_equity = max(peak, account.equity)
    drawdown = account.equity / state.peak_equity - 1
    if (
        drawdown <= -bot.max_drawdown_pct
        and not state.drawdown_tripped
        and mode is not Mode.DRY_RUN
    ):
        bot.kill_switch_file.write_text(
            f"Written by the bot on {datetime.now(UTC):%Y-%m-%d}: the account is {drawdown:.1%} "
            f"below its best level (limit {bot.max_drawdown_pct:.0%}).\n"
            "Look at logs/journal.csv. Delete this file to let the bot buy again.\n",
            encoding="utf-8",
        )
        state.drawdown_tripped = True
        journal.write("safety", f"drawdown stop: {drawdown:.1%} below the best level; wrote STOP")
        attention.append("the drawdown limit wrote the STOP file")
    reasons = []
    stop = find_kill_switch(folder)
    if stop is not None:
        reasons.append(f"the kill switch file {stop.name} exists")
    if account.cash < 0:
        reasons.append("account cash is below zero, so the account is borrowing")
        attention.append("account cash is below zero")
    if account.multiplier > 1:
        journal.write(
            "warning",
            f"this account can borrow (margin multiplier {account.multiplier:g}). "
            "The bot never spends more than its cash, but to be sure, set the account's "
            "max margin multiplier to 1: hsw-bot no-margin",
        )
    return not reasons, "; ".join(reasons)


def _valid_peak(value: float | None) -> float:
    try:
        peak = checked_peak(value)
    except ValueError as err:
        raise FailClosedError(f"The best account level in the records is broken ({err}).") from err
    if peak is None:
        raise FailClosedError("The records have no best account level.")
    return peak


def daily_loss_check(
    account: AccountSnapshot,
    broker: Broker,
    state: BotState,
    bars: Bars,
    market_date: pd.Timestamp,
    cfg: Config,
    mode: Mode,
    journal: Journal,
) -> str:
    """The daily loss limit: today's equity against the close of the trading day before.

    Returns why new buys must stop (the caller also asks for a look), or "".
    If no value for the previous close can be trusted, the limit cannot be
    checked: no new buys. A pretend account (dry run) has no history, so
    there the check is skipped, with a note.
    """
    earlier = bars.dates[bars.dates < market_date]
    if not len(earlier):
        return (
            "there is no earlier trading day in the data, so the daily loss limit cannot be checked"
        )
    previous = pd.Timestamp(earlier[-1]).date()
    sessions = [pd.Timestamp(d).date() for d in bars.dates]
    reference = previous_close(account, broker, state, previous, sessions, journal)
    if reference is None:
        if mode is Mode.DRY_RUN:
            journal.write(
                "note", "pretend account: no previous close to check the daily loss against"
            )
            return ""
        return (
            f"no account value for the previous close ({previous}) can be trusted, so the "
            "daily loss limit cannot be checked"
        )
    value, source = reference
    daily = account.equity / value - 1
    journal.write("daily_loss", f"{daily:+.2%} since the close of {previous} (from {source})")
    if daily <= -cfg.bot.daily_loss_limit_pct:
        return (
            f"the account fell {daily:.1%} since the previous close ({previous}, from {source}); "
            f"the limit is {cfg.bot.daily_loss_limit_pct:.0%}. If you took money out today, "
            "that explains it, and buys start again tomorrow"
        )
    return ""


def previous_close(
    account: AccountSnapshot,
    broker: Broker,
    state: BotState,
    previous: date,
    sessions: Sequence[date],
    journal: Journal,
) -> tuple[float, str] | None:
    """The account value at the close of ``previous``, from the best source available.

    1. Alpaca's portfolio history: the daily value for that very trading day.
    2. The account's ``last_equity``, but only if its date (``balance_asof``)
       is that day. When Alpaca moves that date forward is not documented.
    3. The value this bot saved after its own good run on that evening.

    Never a value from today itself: today against today is always 0%, and
    the limit would never trip.
    """
    try:
        points = broker.get_daily_equity(previous - timedelta(days=HISTORY_LOOKBACK_DAYS))
    except Exception as err:
        journal.write("note", f"could not read the portfolio history: {err}")
        points = []
    found = [v for label, v in points if session_of(label, sessions) == previous]
    if found and math.isfinite(found[-1]) and found[-1] > 0:
        return found[-1], "Alpaca's portfolio history"
    last = account.last_equity
    if account.balance_asof == previous and math.isfinite(last) and last > 0:
        return last, "Alpaca's previous close value"
    saved = state.close_equity.get(previous.isoformat())
    if saved is not None:
        return saved, "the bot's own run that evening"
    return None


def session_of(label: datetime, sessions: Sequence[date]) -> date | None:
    """The trading day a daily portfolio history point belongs to.

    Alpaca labels each daily point with the START of its window and does not
    say in which time zone. In New York time the label is then either on the
    trading day itself (for example 00:00 or 09:30) or on the evening before
    it (00:00 UTC is 19:00 or 20:00 in New York; a window may also start at
    the 16:00 close). So a label at 16:00 or later belongs to the NEXT
    trading day, and an earlier label to its own day. Every likely labeling
    then gives the same answer.
    """
    local = label.astimezone(MARKET_TZ)
    if local.time() >= MARKET_CLOSE:
        later = [d for d in sessions if d > local.date()]
        return later[0] if later else None
    return local.date() if local.date() in set(sessions) else None


def status_line(
    mode: Mode, account: AccountSnapshot, cfg: Config, recon: Reconciled, state: BotState
) -> str:
    """One line that says where things stand."""
    stop = find_kill_switch(cfg.bot.kill_switch_file.parent)
    switch = f"ON ({stop.name})" if stop is not None else "off"
    managed = len(state.positions)
    return (
        f"{mode.value.upper()} | account {account.masked_number} | kill switch {switch} | "
        f"positions: {managed} managed, {len(recon.ignored)} ignored, "
        f"{len(recon.unmanaged)} unmanaged"
    )


# ----------------------------------------------------------------------------- planning


def plan_all(
    ctx: BotContext,
    strategies: Mapping[str, Strategy],
    state: BotState,
    account: AccountSnapshot,
    held: Mapping[str, BrokerPosition],
    recon: Reconciled,
    bars: Bars,
    blocked: set[str],
    in_flight: set[str],
    bad: set[str],
    allow_entries: bool,
) -> list[PlannedOrder]:
    """Ask every strategy for today's orders, through the same planner as the backtest.

    * Carried sells go first. They are market orders for the next open, so
      they need no fresh bar: they go out even when today's bar is bad.
    * Each strategy's slice holds ALL its positions, also those with a bad bar
      or a carried sell, as in the backtest. So it never thinks it has a free
      slot or cash that it does not have. Positions with a bad bar are not
      judged for exits this run (``frozen``).
    """
    cfg, journal = ctx.config, ctx.journal
    prefix = cfg.bot.client_order_prefix
    last = len(bars.dates) - 1
    market_date = pd.Timestamp(bars.dates[last])
    closes = bars.close.ffill().iloc[last]
    prices = {str(s): float(v) for s, v in closes.items()}
    orders: list[PlannedOrder] = []

    for symbol, carried in sorted(state.carried_sells.items()):
        record, position = state.positions.get(symbol), held.get(symbol)
        if record is None or position is None or symbol in in_flight:
            continue  # sold, gone, or an order for it is still working
        qty = min(record.qty, position.qty)
        why = f"sent again ({carried.tries} failed so far): {carried.reason}"
        again = OrderPlan(symbol, "sell", why, qty=qty, signal_date=market_date)
        retry = max(carried.tries, 1)  # a new id the broker has never seen
        cid = make_order_id(prefix, carried.sleeve, market_date, symbol, "sell", retry)
        orders.append(PlannedOrder(carried.sleeve, again, cid, prices.get(symbol, math.nan), True))
        journal.write("decision", why, sleeve=carried.sleeve, symbol=symbol, side="sell", qty=qty)

    sleeve_days = []
    for sleeve, share in cfg.bot.sleeves.items():
        strategy = strategies[sleeve]
        positions = {
            symbol: Position(
                symbol=symbol,
                qty=min(record.qty, held[symbol].qty),
                entry_price=record.entry_price,
                entry_date=pd.Timestamp(record.entry_date),
                signal_date=pd.Timestamp(record.signal_date),
            )
            for symbol, record in state.positions.items()
            if record.sleeve == sleeve and symbol in held
        }
        day = Day(last, bars.dates, build_table(strategy, bars))
        sleeve_days.append(
            SleeveDay(
                sleeve,
                strategy,
                cfg.strategy(sleeve).sizing,
                share,
                day,
                positions,
                frozen=frozenset(positions) & bad,
            )
        )
    plan = plan_sleeves(
        sleeve_days,
        prices,
        cfg.costs,
        account_cash=account.cash,
        equity=account.equity - recon.others_value,
        allow_entries=allow_entries,
        blocked=blocked,
    )
    for sleeve, symbol, why in plan.skipped:
        journal.write("skip_entry", why, sleeve=sleeve, symbol=symbol)
    for sleeve, order in plan.orders:
        if order.side == "sell" and (
            order.symbol in in_flight or order.symbol in state.carried_sells
        ):
            why = (
                "an order for it is still working"
                if order.symbol in in_flight
                else "its earlier sell is being sent again"
            )
            journal.write("skip_order", why, sleeve=sleeve, symbol=order.symbol, side="sell")
            continue
        cid = make_order_id(prefix, sleeve, market_date, order.symbol, order.side)
        price = prices.get(order.symbol, math.nan)
        orders.append(PlannedOrder(sleeve, order, cid, price))
        journal.write(
            "decision",
            order.reason,
            sleeve=sleeve,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            notional=order.notional,
            price=price,
        )
    return orders


def drop_already_sent(
    orders: list[PlannedOrder], state: BotState, journal: Journal
) -> list[PlannedOrder]:
    """Idempotent: an order id we already sent is never sent again (a second run sends nothing)."""
    sent = set(state.sent_ids)
    kept = []
    for order in orders:
        if order.client_order_id in sent:
            journal.write(
                "skip_order",
                "already sent for this signal day",
                sleeve=order.sleeve,
                symbol=order.plan.symbol,
                side=order.plan.side,
            )
        else:
            kept.append(order)
    return kept


def check_assets(
    orders: list[PlannedOrder], broker: Broker, journal: Journal
) -> list[PlannedOrder]:
    """Buys need a tradable, fractionable stock (we buy in dollars, not whole shares)."""
    kept = []
    for order in orders:
        if order.plan.side == "buy":
            try:
                asset = broker.get_asset(order.plan.symbol)
            except Exception as err:
                journal.write(
                    "skip_order",
                    f"could not check the stock: {err}",
                    sleeve=order.sleeve,
                    symbol=order.plan.symbol,
                    side="buy",
                )
                continue
            if not (asset.tradable and asset.fractionable):
                journal.write(
                    "skip_order",
                    "the broker cannot trade it in fractions",
                    sleeve=order.sleeve,
                    symbol=order.plan.symbol,
                    side="buy",
                )
                continue
        kept.append(order)
    return kept


def guard_orders(
    orders: list[PlannedOrder],
    account: AccountSnapshot,
    held: Mapping[str, BrokerPosition],
    state: BotState,
    recon: Reconciled,
    cfg: Config,
) -> None:
    """Last checks before money moves. If one trips, there is a bug: fail closed.

    These should never trip. They are here because a bug in a new strategy
    should cost you a day of trading, never a pile of money.
    """
    seen: set[tuple[str, str]] = set()
    spend = 0.0
    equity = account.equity - recon.others_value
    for order in orders:
        plan, key = order.plan, (order.plan.symbol, order.plan.side)
        if key in seen:
            raise FailClosedError(f"Bug guard: two {plan.side} orders for {plan.symbol}.")
        seen.add(key)
        if plan.side == "sell":
            position, record = held.get(plan.symbol), state.positions.get(plan.symbol)
            own = min(position.qty, record.qty) if position and record else 0.0
            if (plan.qty or 0) > own + QTY_TOLERANCE:
                raise FailClosedError(f"Bug guard: selling more {plan.symbol} than the bot owns.")
        else:
            share = cfg.bot.sleeves[order.sleeve]
            cap = equity * share * cfg.strategy(order.sleeve).sizing.max_position_pct
            amount = plan.notional or 0.0
            if not (math.isfinite(amount) and 0 < amount <= cap * SIZE_TOLERANCE):
                raise FailClosedError(
                    f"Bug guard: a buy of ${amount:,.2f} in {plan.symbol} is over its cap."
                )
            spend += amount
    if spend > max(account.cash, 0.0) + QTY_TOLERANCE:
        raise FailClosedError(f"Bug guard: buys of ${spend:,.2f} would spend more than the cash.")
    if {s for s, side in seen if side == "buy"} & {s for s, side in seen if side == "sell"}:
        raise FailClosedError("Bug guard: buying and selling the same stock in one run.")


# ----------------------------------------------------------------------------- send, read back


def submit_all(
    orders: list[PlannedOrder],
    broker: Broker,
    state: BotState,
    ctx: BotContext,
    journal: Journal,
    progress: Progress,
    attention: list[str],
) -> tuple[list[BrokerOrder], int, int]:
    """Send sells first, then buys. Each order goes into the records BEFORE it is sent.

    If sending fails in an unclear way (a timeout, a dropped connection), the
    order may still have reached the broker. So we ask the broker by order id.
    If it is there, we track it. If we cannot tell, the record stays as
    "submitting" and the next run asks again. A sell the broker clearly does
    not have is carried, and sent again by the next run. Returns the orders,
    how many stayed unclear, and how many the broker refused outright.
    """
    results: list[BrokerOrder] = []
    unclear = 0
    refused = 0
    for order in sorted(orders, key=lambda o: o.plan.side != "sell"):
        plan, cid = order.plan, order.client_order_id
        pending = PendingOrder(
            client_order_id=cid,
            sleeve=order.sleeve,
            symbol=plan.symbol,
            side=plan.side,
            signal_date=(plan.signal_date or pd.Timestamp.now()).strftime("%Y-%m-%d"),
            submitted_at=datetime.now(UTC).isoformat(timespec="seconds"),
            qty=plan.qty,
            notional=plan.notional if plan.qty is None else None,
            status=SUBMITTING,
            reason=plan.reason,
        )
        state.pending[cid] = pending
        state.remember_sent(cid)
        save_state(state, ctx.state_path)
        request = OrderRequest(plan.symbol, plan.side, cid, qty=plan.qty, notional=pending.notional)
        try:
            sent: BrokerOrder | None = broker.submit_order(request)
        except DuplicateOrderError:
            sent, _ = _ask(broker, cid)
        except Exception as err:
            journal.write(
                "order_error",
                f"sending failed: {err}; asking the broker if it arrived",
                sleeve=order.sleeve,
                symbol=plan.symbol,
                side=plan.side,
            )
            sent, answered = _ask(broker, cid)
            if sent is None and answered:  # the broker clearly does not have it
                del state.pending[cid]
                if plan.side == "sell":
                    since = pending.signal_date
                    why = f"the broker refused it: {err}"
                    carry_sell(
                        state,
                        order.sleeve,
                        plan.symbol,
                        plan.reason,
                        since,
                        why,
                        journal,
                        attention,
                    )
                save_state(state, ctx.state_path)
                refused += 1
                continue
        if sent is None:
            progress.sent += 1  # it may have arrived: the next run checks
            unclear += 1
            journal.write(
                "order_problem",
                "unclear whether the broker has this order; the next run checks",
                sleeve=order.sleeve,
                symbol=plan.symbol,
                side=plan.side,
            )
            continue
        progress.sent += 1
        pending.status = SENT  # a carried sell stays carried until its fill is read back
        save_state(state, ctx.state_path)
        journal.write(
            "order_sent",
            plan.reason,
            sleeve=order.sleeve,
            symbol=plan.symbol,
            side=plan.side,
            qty=plan.qty,
            notional=plan.notional,
            status=sent.status,
        )
        results.append(sent)
    return results, unclear, refused


def _ask(broker: Broker, client_id: str) -> tuple[BrokerOrder | None, bool]:
    """Ask the broker for an order by id: (the order or None, did the broker answer at all)."""
    try:
        return broker.get_order(client_id), True
    except Exception:
        return None, False


def read_back(
    results: list[BrokerOrder], broker: Broker, ctx: BotContext, journal: Journal
) -> list[BrokerOrder]:
    """Ask the broker about every order we just sent, until each has a clear answer.

    In the evening the usual answer is "accepted": queued for the next open.
    That is NOT a fill. The fill is confirmed on the next run, from the
    broker's own numbers (see ``resolve_pending``).
    """
    waited = 0.0
    latest = {o.client_order_id: o for o in results}
    while True:
        for client_id in list(latest):
            fresh, answered = _ask(broker, client_id)
            if fresh is not None:
                latest[client_id] = fresh
            elif not answered:
                journal.write("order_problem", "could not read the order back", symbol=client_id)
        unsettled = [o for o in latest.values() if o.status in UNSETTLED]
        if not unsettled or waited >= ctx.config.bot.order_poll_seconds:
            break
        ctx.sleep(ctx.config.bot.order_poll_interval)
        waited += ctx.config.bot.order_poll_interval
    for order in latest.values():
        meaning = {
            "accepted": "queued for the next open (not filled yet; the next run confirms the fill)",
            "new": "at the exchange, waiting to fill",
            "filled": "filled",
        }.get(order.status, f"needs a look: {order.status}")
        journal.write(
            "read_back", meaning, symbol=order.symbol, side=order.side, status=order.status
        )
    return list(latest.values())
