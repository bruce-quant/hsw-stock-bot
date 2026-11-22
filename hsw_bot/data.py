"""Daily price bars: download them, keep a local copy, and check them.

We use Yahoo Finance through the ``yfinance`` package. It is free and needs no
account. The prices are "adjusted": splits and dividends are folded into the
history, so a return measured on these prices is the return an owner of the
stock really got. The backtest and the live bot read the same adjusted bars,
so they see the same numbers.

``yfinance`` is not an official Yahoo product. Sometimes it returns a missing
or broken bar. That is why ``check_latest`` exists, and why the bot refuses to
trade when the newest bars look wrong.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

FIELDS = ("open", "high", "low", "close", "volume")
CACHE_META = "_download.json"


@dataclass(frozen=True)
class Bars:
    """Daily bars for many symbols, lined up on the same dates.

    Each field is a table with one row per trading day and one column per
    symbol. A missing value (NaN) means the symbol had no bar that day, for
    example because it was not listed yet.
    """

    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame

    def __post_init__(self) -> None:
        for name in FIELDS:
            frame: pd.DataFrame = getattr(self, name)
            if not frame.index.equals(self.close.index) or not frame.columns.equals(
                self.close.columns
            ):
                raise ValueError(f"Bars.{name} does not line up with Bars.close.")

    @property
    def dates(self) -> pd.DatetimeIndex:
        """All trading days in the data, oldest first."""
        return pd.DatetimeIndex(self.close.index)

    @property
    def symbols(self) -> list[str]:
        return [str(c) for c in self.close.columns]

    def select(self, symbols: Iterable[str]) -> Bars:
        """The same bars for a subset of symbols."""
        keep = [s for s in symbols if s in self.close.columns]
        return Bars(*(getattr(self, name)[keep] for name in FIELDS))

    def until(self, last: pd.Timestamp) -> Bars:
        """Only the bars up to and including ``last``. Used to prove there is no peeking."""
        return Bars(*(getattr(self, name).loc[:last] for name in FIELDS))

    def drop_dates(self, dates: Iterable[pd.Timestamp]) -> Bars:
        """The same bars without the given days (used to drop a half finished day)."""
        drop = [d for d in dates if d in self.close.index]
        return Bars(*(getattr(self, name).drop(index=drop) for name in FIELDS))

    @classmethod
    def from_frames(cls, frames: Mapping[str, pd.DataFrame]) -> Bars:
        """Build Bars from one table per symbol (columns open, high, low, close, volume)."""
        if not frames:
            raise ValueError("No price data to build bars from.")
        cleaned = {symbol: clean_frame(frame) for symbol, frame in frames.items()}
        index = pd.DatetimeIndex(sorted(set().union(*(f.index for f in cleaned.values()))))
        tables = {
            name: pd.DataFrame(
                {symbol: frame[name].reindex(index) for symbol, frame in cleaned.items()},
                index=index,
                dtype="float64",
            )
            for name in FIELDS
        }
        return cls(**tables)


def clean_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Tidy one symbol's bars so the rest of the code can trust them.

    * column names become lower case;
    * rows without a positive close are dropped;
    * a missing open is set to the close (rare, but it happens);
    * high and low are widened if rounding left them inside the open or close.
    """
    df = frame.copy()
    df.columns = [str(c).lower() for c in df.columns]
    missing = [name for name in FIELDS if name not in df.columns]
    if missing:
        raise ValueError(f"Price table is missing columns {missing}.")
    df = df[list(FIELDS)].astype("float64")
    index = pd.to_datetime(df.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    df.index = index.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df["close"].gt(0)]
    df["open"] = df["open"].where(df["open"].gt(0), df["close"])
    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)
    df["volume"] = df["volume"].fillna(0.0)
    return df


# ----------------------------------------------------------------------------- download


def download(symbols: Sequence[str], start: str, end: str | None = None) -> dict[str, pd.DataFrame]:
    """Download adjusted daily bars from Yahoo Finance. Needs the internet.

    Symbols that fail come back missing from the result (we never invent data).
    """
    import yfinance as yf  # imported here so tests and dry runs can start without it

    if not symbols:
        return {}
    raw = yf.download(
        tickers=list(symbols),
        start=start,
        end=end,
        auto_adjust=True,
        actions=False,
        group_by="ticker",
        progress=False,
        threads=True,
        multi_level_index=True,
    )
    frames: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return frames
    for symbol in symbols:
        if isinstance(raw.columns, pd.MultiIndex):
            if symbol not in raw.columns.get_level_values(0):
                continue
            table = raw[symbol]
        else:
            table = raw
        table = table.dropna(how="all")
        if not table.empty:
            frames[symbol] = table
    return frames


def load_history(
    symbols: Sequence[str],
    start: str,
    cache_dir: Path,
    *,
    refresh: bool = False,
) -> Bars:
    """Bars for backtests, read from the local copy in ``data/`` when we have it.

    If any symbol is missing from the copy, or ``refresh`` is True, everything
    is downloaded again in one go. That keeps every symbol on the same end date.
    """
    wanted = sorted(set(symbols))
    cached = {s: cache_dir / f"{_safe_name(s)}.csv" for s in wanted}
    if refresh or not all(path.is_file() for path in cached.values()):
        log.info("Downloading %d symbols from Yahoo Finance (start %s)...", len(wanted), start)
        frames = download(wanted, start)
        if not frames:
            raise RuntimeError(
                "Yahoo Finance returned no data. Check your internet connection and try again."
            )
        cache_dir.mkdir(parents=True, exist_ok=True)
        for symbol, frame in frames.items():
            clean_frame(frame).to_csv(cached[symbol], index_label="date")
        failed = sorted(set(wanted) - set(frames))
        meta = {
            "downloaded_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "start": start,
            "symbols": wanted,
            "failed": failed,
        }
        (cache_dir / CACHE_META).write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if failed:
            log.warning("No data for %s. They are left out.", ", ".join(failed))
    frames = {
        s: pd.read_csv(path, index_col="date", parse_dates=True)
        for s, path in cached.items()
        if path.is_file()
    }
    return Bars.from_frames(frames)


def load_recent(symbols: Sequence[str], days: int, now: datetime) -> Bars:
    """Fresh bars for a live run: the last ``days`` calendar days, never from the cache."""
    start = (pd.Timestamp(now).tz_localize(None) - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    frames = download(sorted(set(symbols)), start)
    if not frames:
        raise RuntimeError("Yahoo Finance returned no data. Check the internet connection.")
    return Bars.from_frames(frames)


def cache_info(cache_dir: Path) -> dict[str, object]:
    """What the last download recorded (time, start, failed symbols), or {} if none."""
    path = cache_dir / CACHE_META
    if not path.is_file():
        return {}
    loaded: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def to_float(value: object) -> float:
    """Any number from a table as a plain float. Anything else becomes NaN (not a number)."""
    if isinstance(value, int | float | np.integer | np.floating):
        return float(value)
    return math.nan


def _safe_name(symbol: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in symbol)


# ----------------------------------------------------------------------------- checks


@dataclass(frozen=True)
class DataCheck:
    """The health of the newest trading day for each symbol we care about.

    * ``missing``: no bar for that symbol on the newest day (late or failed data);
    * ``broken``: a bar exists, but a price is not a number or not above zero.
    """

    market_date: pd.Timestamp
    good: tuple[str, ...]
    missing: tuple[str, ...]
    broken: tuple[str, ...]

    @property
    def bad(self) -> frozenset[str]:
        return frozenset(self.missing) | frozenset(self.broken)


def check_latest(bars: Bars, symbols: Iterable[str], market_date: pd.Timestamp) -> DataCheck:
    """Check that each symbol has a sane bar on ``market_date``, the newest trading day."""
    good: list[str] = []
    missing: list[str] = []
    broken: list[str] = []
    for symbol in sorted(set(symbols)):
        if symbol not in bars.close.columns or market_date not in bars.close.index:
            missing.append(symbol)
            continue
        close = to_float(bars.close.at[market_date, symbol])
        if math.isnan(close):
            missing.append(symbol)
            continue
        prices = [
            to_float(getattr(bars, f).at[market_date, symbol]) for f in ("open", "high", "low")
        ]
        prices.append(close)
        if all(math.isfinite(v) and v > 0 for v in prices):
            good.append(symbol)
        else:
            broken.append(symbol)
    return DataCheck(market_date, tuple(good), tuple(missing), tuple(broken))
