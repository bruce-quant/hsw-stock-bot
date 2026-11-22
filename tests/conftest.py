"""Shared test fixtures. Every test runs offline on made up data."""

from __future__ import annotations

import pytest

from hsw_bot.data import Bars
from tests import synthetic


@pytest.fixture(scope="session")
def bars() -> Bars:
    """The fake market used by most tests (built once, never changed)."""
    return synthetic.make_bars()


@pytest.fixture(scope="session")
def gappy_bars(bars: Bars) -> Bars:
    """The fake market with holes: a missing week, a late listing and an early delisting."""
    frames = {}
    for symbol in bars.symbols:
        frame = (
            bars.open[[symbol]]
            .rename(columns={symbol: "open"})
            .assign(
                high=bars.high[symbol],
                low=bars.low[symbol],
                close=bars.close[symbol],
                volume=bars.volume[symbol],
            )
        )
        # every symbol misses a few days somewhere, so held positions meet gaps
        k = bars.symbols.index(symbol)
        frame = frame.drop(index=frame.index[[200 + 23 * k, 201 + 23 * k, 420 + 17 * k]])
        if symbol == "S02":
            frame = frame.drop(index=frame.index[300:305])  # a week with no data
        if symbol == "S03":
            frame = frame.iloc[250:]  # listed late
        if symbol == "S04":
            frame = frame.iloc[:600]  # delisted early
        frames[symbol] = frame
    return Bars.from_frames(frames)
