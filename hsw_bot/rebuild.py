"""Order ids, and rebuilding lost records from the broker's order history.

Every order the bot sends gets an id like ``hsw-swing-20261120-BRK-B-buy``:

    prefix - strategy - signal day - symbol - side [- retry]

A sell that is sent again after a failure gets a retry number at the end
(``...-sell-r1``, ``...-sell-r2``), because a broker may refuse an id it has
already seen, even for an order it rejected.

The bot NEVER takes a position from the order history during a normal run.
A run manages only the positions in its own records, and positions enter the
records only from the bot's own orders (each one written down before it is
sent). Replaying the history is a manual step, ``hsw-bot rebuild-state``, for
when the records file is lost. It is careful on purpose:

* it replays EVERY fill in each stock you hold, yours and the bot's, in time
  order, and after each fill it caps the bot's shares at what the account
  really held at that moment;
* if you traded a stock by hand after the bot's last fill in it, it does not
  claim that stock at all. It lists it, so you can decide: add it to
  ``bot.ignore_symbols``, or close it by hand.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from hsw_bot.broker.base import Broker, BrokerOrder, BrokerPosition
from hsw_bot.state import SENT, BotState, PendingOrder, PositionRecord, checked_peak

MARKET_TZ = ZoneInfo("America/New_York")
QTY_TOLERANCE = 1e-6
ID_PARTS = 5  # prefix, strategy, day, symbol (may contain "-"), side
RETRY = re.compile(r"r(\d+)")  # the retry part of a sell sent again: r1, r2, ...
NEVER = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class OrderId:
    """The parts of one of the bot's order ids."""

    prefix: str
    sleeve: str
    signal_date: str  # YYYY-MM-DD
    symbol: str
    side: str
    retry: int = 0  # 0 for a first try


def make_order_id(
    prefix: str, sleeve: str, day: pd.Timestamp, symbol: str, side: str, retry: int = 0
) -> str:
    """hsw-swing-20261120-AAPL-buy. The same decision always gets the same id.

    A retry (a sell sent again after it failed) gets ``-r<n>`` at the end, so
    every try has an id the broker has never seen.
    """
    base = f"{prefix}-{sleeve}-{day:%Y%m%d}-{symbol}-{side}"
    return f"{base}-r{retry}" if retry > 0 else base


def parse_order_id(client_order_id: str, prefix: str) -> OrderId | None:
    """Split one of our order ids into its parts. None means it is not one of ours."""
    parts = client_order_id.split("-")
    retry = 0
    found = RETRY.fullmatch(parts[-1]) if len(parts) > ID_PARTS else None
    if found is not None:
        retry = int(found.group(1))
        parts = parts[:-1]
    if len(parts) < ID_PARTS or parts[0] != prefix or parts[-1] not in ("buy", "sell"):
        return None
    day = parts[2]
    if not (day.isdigit() and len(day) == len("YYYYMMDD")):
        return None
    symbol = "-".join(parts[3:-1])
    signal = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    return OrderId(prefix, parts[1], signal, symbol, parts[-1], retry)


def market_day(moment: datetime | None) -> str:
    """The trading day (New York date) of a fill time, or "" if the broker gave none."""
    if moment is None:
        return ""
    return moment.astimezone(MARKET_TZ).date().isoformat()


def fill_time(order: BrokerOrder) -> datetime:
    """When an order filled (or, if the broker did not say, when it was sent)."""
    return order.filled_at or order.submitted_at or NEVER


# ----------------------------------------------------------------------------- the replay


@dataclass(frozen=True)
class Replay:
    """What the order history says about one stock the account holds."""

    symbol: str
    held: float  # shares at the broker now
    bot: float  # shares the replay gives the bot
    record: PositionRecord | None  # the record to keep; None means "not claimed"
    why: str  # in plain words, for the table rebuild-state prints

    @property
    def claimed(self) -> bool:
        return self.record is not None


def replay_history(
    orders: Sequence[BrokerOrder], prefix: str, held: Mapping[str, BrokerPosition]
) -> list[Replay]:
    """Replay every fill in each stock the account holds, and decide whose shares they are.

    The bot's share count goes up with its own buys and down with its own
    sells. After EVERY fill, yours or the bot's, it is capped at what the
    account held at that moment. So when you sell shares by hand, the bot's
    count can only go down, and shares you buy later are never the bot's.
    """
    replays: list[Replay] = []
    for symbol, position in sorted(held.items()):
        fills = sorted(
            (o for o in orders if o.symbol == symbol and o.filled_qty > 0), key=fill_time
        )
        running = bot = 0.0
        record: PositionRecord | None = None
        last_bot: datetime | None = None
        last_yours: datetime | None = None
        for order in fills:
            parsed = parse_order_id(order.client_order_id, prefix)
            change = order.filled_qty if order.side == "buy" else -order.filled_qty
            running = max(running + change, 0.0)
            if parsed is None:
                last_yours = fill_time(order)
            else:
                last_bot = fill_time(order)
                if order.side == "buy":
                    record = _add_buy(record, bot, order, parsed)
                    bot += order.filled_qty
                else:
                    bot -= order.filled_qty
            bot = min(bot, running)
            if bot <= QTY_TOLERANCE:
                bot, record = 0.0, None
        replays.append(_verdict(symbol, position.qty, bot, record, last_bot, last_yours))
    return replays


def _add_buy(
    record: PositionRecord | None, bot: float, order: BrokerOrder, parsed: OrderId
) -> PositionRecord:
    price = order.filled_avg_price or 0.0
    if record is None:
        return PositionRecord(
            sleeve=parsed.sleeve,
            symbol=order.symbol,
            qty=order.filled_qty,
            entry_price=price,
            entry_date=market_day(order.filled_at),
            signal_date=parsed.signal_date,
        )
    total = bot + order.filled_qty
    record.entry_price = (bot * record.entry_price + order.filled_qty * price) / total
    return record


def _verdict(
    symbol: str,
    held: float,
    bot: float,
    record: PositionRecord | None,
    last_bot: datetime | None,
    last_yours: datetime | None,
) -> Replay:
    if record is None or bot <= QTY_TOLERANCE:
        why = "the bot never bought it" if last_bot is None else "the bot's shares were all sold"
        return Replay(symbol, held, 0.0, None, why)
    if last_bot is not None and last_yours is not None and last_yours > last_bot:
        why = (
            f"you traded it by hand on {market_day(last_yours)}, after the bot's last fill; "
            "not claimed. Add it to bot.ignore_symbols, or close it by hand"
        )
        return Replay(symbol, held, bot, None, why)
    record.qty = min(bot, held)
    if not (math.isfinite(record.qty) and record.qty > 0):
        return Replay(symbol, held, 0.0, None, "no shares left to claim")
    return Replay(symbol, held, record.qty, record, "bought by the bot, never traded by hand")


# ----------------------------------------------------------------------------- rebuild-state


class RebuildError(Exception):
    """The records cannot be rebuilt safely. The message says what to do."""


@dataclass(frozen=True)
class Rebuilt:
    """New records, and what went into them."""

    state: BotState
    replays: list[Replay]  # one per stock the account holds (empty with --fresh-start)
    highest: tuple[date, float] | None  # the highest daily account value since the bot began


def rebuild_state(
    broker: Broker,
    prefix: str,
    *,
    peak: float | None,
    peak_now: bool,
    fresh_start: bool,
) -> Rebuilt:
    """Build fresh records for the broker's account from its own history.

    * Positions: ``replay_history`` (see above). With ``fresh_start`` every
      current position is declared yours instead, and the bot leaves it alone.
    * Open orders with the bot's prefix become pending orders again.
    * The best account level (for the drawdown limit) is YOUR choice: a dollar
      amount (``peak``) or today's equity (``peak_now``). The old automatic
      guess from the portfolio history counted deposits and withdrawals as
      gains and losses, so it is shown only as information.
    """
    account = broker.get_account()
    held = {p.symbol: p for p in broker.get_positions()}
    history = broker.get_order_history()
    highest = highest_since_first_fill(broker, history, prefix)
    if (peak is None) == (not peak_now):
        hint = ""
        if highest is not None:
            hint = (
                f" For information: the highest daily account value since the bot's first "
                f"fill was ${highest[1]:,.2f} on {highest[0]} (deposits and withdrawals "
                "included)."
            )
        raise RebuildError(
            "Choose the best account level for the drawdown limit: --peak <dollars> (for "
            "example the value from your old records), or --peak-now (today's equity), "
            f"not both.{hint}"
        )
    try:
        chosen = checked_peak(account.equity if peak_now else peak)
    except ValueError as err:
        raise RebuildError(f"That best level cannot be used ({err}).") from err
    replays = [] if fresh_start else replay_history(history, prefix, held)
    records = {r.symbol: r.record for r in replays if r.record is not None}
    pending: dict[str, PendingOrder] = {}
    sent_ids: list[str] = []
    for order in history:
        parsed = parse_order_id(order.client_order_id, prefix)
        if parsed is None:
            continue
        sent_ids.append(order.client_order_id)
        if order.is_open:
            pending[order.client_order_id] = PendingOrder(
                client_order_id=order.client_order_id,
                sleeve=parsed.sleeve,
                symbol=order.symbol,
                side=order.side,
                signal_date=parsed.signal_date,
                submitted_at=order.submitted_at.isoformat() if order.submitted_at else "",
                qty=order.qty,
                notional=order.notional,
                status=SENT,
            )
    state = BotState(
        account_id=account.account_id,
        positions=records,
        pending=pending,
        sent_ids=sent_ids,
        own_symbols=sorted(held) if fresh_start else [],
        peak_equity=chosen,
    )
    return Rebuilt(state, replays, highest)


def highest_since_first_fill(
    broker: Broker, history: Sequence[BrokerOrder], prefix: str
) -> tuple[date, float] | None:
    """The highest daily account value since the bot's first fill, if the broker can say."""
    fills = [
        fill_time(o)
        for o in history
        if o.filled_qty > 0 and parse_order_id(o.client_order_id, prefix) is not None
    ]
    if not fills:
        return None
    try:
        points = broker.get_daily_equity(min(fills).astimezone(MARKET_TZ).date())
    except Exception:  # information only: never a reason to fail
        return None
    if not points:
        return None
    label, value = max(points, key=lambda point: point[1])
    return label.astimezone(MARKET_TZ).date(), value
