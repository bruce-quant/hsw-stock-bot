"""The bot's own records, kept in a small JSON file between runs.

The broker knows WHAT the account holds. It does not know WHY: which strategy
bought a stock, and on which evening it decided to. The exit rules need that
(a time stop counts days from the entry). So the bot keeps its own records.

Rules that keep the records honest:

* Each mode has its own file (``state/paper/`` and ``state/live/``), and each
  file names the one broker account it belongs to. Records from one account
  are never applied to another.
* Every run checks the records against the broker ("reconcile"). The bot
  never sells more shares than its records say it bought.
* A missing file is only a "first run" when the account is empty. Otherwise
  the bot refuses to trade until ``hsw-bot rebuild-state`` rebuilds them.
* Positions only ever enter the records from the bot's OWN orders: each order
  is written down before it is sent, and its fill is read back later. A run
  never takes a position from the order history.
* The file is written safely: first to a temporary file, then renamed over the
  old one, so a crash can never leave half a file behind. The previous version
  is kept as ``.bak``.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
MAX_SENT_IDS = 5000  # how many past order ids we remember, to never send one twice
MAX_CLOSES = 10  # how many past evenings' account values we keep (only the last one is used)
MAX_MOVEMENTS = 1000  # how many deposit and withdrawal ids we remember, so each counts once
SUBMITTING = "submitting"  # written just BEFORE an order goes out
SENT = "sent"  # the broker has the order


class StateError(Exception):
    """The records exist but cannot be trusted. The bot refuses to trade blind."""


@dataclass
class PositionRecord:
    """A position this bot opened, and why."""

    sleeve: str
    symbol: str
    qty: float
    entry_price: float
    entry_date: str  # YYYY-MM-DD, the day the buy filled ("" until the bars show it)
    signal_date: str  # YYYY-MM-DD, the evening the strategy decided to buy


@dataclass
class PendingOrder:
    """An order we sent (or were about to send) that we have not yet seen finish."""

    client_order_id: str
    sleeve: str
    symbol: str
    side: str
    signal_date: str
    submitted_at: str
    qty: float | None = None
    notional: float | None = None
    status: str = SUBMITTING
    reason: str = ""  # why it was sent (kept, so a sell sent again still says why)


@dataclass
class CarriedSell:
    """A sell that did not fill. The next run sends it again, without judging it again."""

    sleeve: str
    symbol: str
    reason: str
    since: str  # YYYY-MM-DD of the evening it was first decided
    tries: int = 0  # how many times it has failed so far (each new try gets a new order id)


@dataclass
class BotState:
    account_id: str | None = None
    positions: dict[str, PositionRecord] = field(default_factory=dict)
    pending: dict[str, PendingOrder] = field(default_factory=dict)
    carried_sells: dict[str, CarriedSell] = field(default_factory=dict)
    sent_ids: list[str] = field(default_factory=list)
    own_symbols: list[str] = field(default_factory=list)  # positions you declared as yours
    peak_equity: float | None = None
    drawdown_tripped: bool = False
    fail_streak: int = 0  # failed (closed) runs in a row
    last_run_utc: str | None = None
    # The account value after each good evening run, by trading day (YYYY-MM-DD).
    # The daily loss limit falls back on it when the broker cannot say.
    close_equity: dict[str, float] = field(default_factory=dict)
    movements_checked: str | None = None  # the last day deposits and withdrawals were read
    seen_movements: list[str] = field(default_factory=list)  # their ids, so each counts once

    def remember_sent(self, client_order_id: str) -> None:
        self.sent_ids.append(client_order_id)
        del self.sent_ids[:-MAX_SENT_IDS]

    def remember_close(self, day: str, equity: float) -> None:
        self.close_equity[day] = _amount(equity, f"close_equity {day}")
        for old in sorted(self.close_equity)[:-MAX_CLOSES]:
            del self.close_equity[old]

    def remember_movements(self, ids: list[str]) -> None:
        self.seen_movements.extend(ids)
        del self.seen_movements[:-MAX_MOVEMENTS]


def load_state(path: Path) -> BotState | None:
    """Read the records. None means there is no file. A broken file is an error."""
    if not path.exists():
        return None
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema") != SCHEMA_VERSION:
            raise ValueError(f"unknown schema {raw.get('schema')!r}")
        state = BotState(
            account_id=_optional_text(raw["account_id"], "account_id"),
            positions={k: PositionRecord(**v) for k, v in raw["positions"].items()},
            pending={k: PendingOrder(**v) for k, v in raw["pending"].items()},
            carried_sells={k: CarriedSell(**v) for k, v in raw["carried_sells"].items()},
            sent_ids=[str(x) for x in raw["sent_ids"]],
            own_symbols=[str(x) for x in raw["own_symbols"]],
            peak_equity=checked_peak(raw["peak_equity"]),
            drawdown_tripped=_flag(raw["drawdown_tripped"]),
            fail_streak=_count(raw["fail_streak"], "fail_streak"),
            last_run_utc=_optional_text(raw["last_run_utc"], "last_run_utc"),
            # Added later: older files simply do not have these yet.
            close_equity={
                str(k): _amount(v, f"close_equity {k}")
                for k, v in raw.get("close_equity", {}).items()
            },
            movements_checked=_optional_day(raw.get("movements_checked"), "movements_checked"),
            seen_movements=[str(x) for x in raw.get("seen_movements", [])],
        )
        for record in state.positions.values():
            _check_record(record)
        for carried in state.carried_sells.values():
            _count(carried.tries, f"the tries of the carried sell of {carried.symbol}")
        return state
    except (ValueError, KeyError, TypeError, AttributeError) as err:
        raise StateError(
            f"The bot's records in {path} cannot be trusted ({err}). Nothing was traded. "
            f"A backup may be in {path.with_suffix('.json.bak')}. Restore it, or rebuild the "
            "records from the broker with: hsw-bot rebuild-state --replace --peak-now"
        ) from err


def save_state(state: BotState, path: Path) -> None:
    """Write the records safely (temporary file, then an atomic rename)."""
    if state.peak_equity is not None:
        checked_peak(state.peak_equity)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA_VERSION, **asdict(state)}
    text = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(text, encoding="utf-8")
    if path.exists():
        backup = path.with_suffix(".json.bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    temporary.replace(path)


def checked_peak(value: object) -> float | None:
    """The account's best level must be a real number above zero (or not set yet)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"peak_equity must be a number, not {value!r}")
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"peak_equity must be a real number above 0, not {value!r}")
    return float(value)


def _check_record(record: PositionRecord) -> None:
    if not (math.isfinite(record.qty) and record.qty > 0):
        raise ValueError(f"position {record.symbol} has a bad share count {record.qty!r}")
    if not (math.isfinite(record.entry_price) and record.entry_price >= 0):
        raise ValueError(f"position {record.symbol} has a bad entry price {record.entry_price!r}")


def _optional_text(value: object, name: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"{name} must be text")


def _optional_day(value: object, name: str) -> str | None:
    text = _optional_text(value, name)
    if text is not None:
        date.fromisoformat(text)  # raises ValueError if it is not YYYY-MM-DD
    return text


def _flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError("drawdown_tripped must be true or false")


def _count(value: object, name: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ValueError(f"{name} must be a whole number, 0 or more")


def _amount(value: object, name: str) -> float:
    """A dollar amount that must be a real number above zero."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number, not {value!r}")
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a real number above 0, not {value!r}")
    return float(value)
