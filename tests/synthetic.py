"""Made up price data for the tests.

Tests must never need the internet, and must give the same answer every time.
So we build fake markets from a fixed random seed. The fake stocks trend up
with regular sharp dips (so the Swing strategy has work to do), and the fake
market has a long fall in the middle (so Steady's trend filter switches).

Only plain arithmetic is used (no exp or log), so the numbers are the same on
every computer down to the last digit.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hsw_bot.config import CostModel, SizingRules
from hsw_bot.data import Bars
from hsw_bot.strategies.my_strategy import MyStrategy, MyStrategyParams
from hsw_bot.strategies.steady import Steady, SteadyParams
from hsw_bot.strategies.swing import Swing, SwingParams

STOCKS = tuple(f"S{k:02d}" for k in range(1, 13))
FUNDS = ("F01", "F02", "F03", "F04", "F05")
MARKET = "MKT"
SAFE = "SAFE"
ALL_SYMBOLS = (*STOCKS, *FUNDS, MARKET, SAFE)

DAYS = 800
SEED = 11
START = "2018-01-01"
MARKET_FALL = (420, 520)  # days when the fake market falls
DIP_EVERY = 45
DIP_LENGTH = 3


def noise(rng: np.random.Generator, n: int) -> np.ndarray:
    """Bell shaped noise with mean 0 and spread 1, from plain sums of uniforms."""
    return (rng.random(n) + rng.random(n) + rng.random(n) - 1.5) * 2.0


def make_bars(days: int = DAYS, seed: int = SEED, start: str = START) -> Bars:
    """A fake market with every symbol in ALL_SYMBOLS."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=days)
    frames: dict[str, pd.DataFrame] = {}
    for k, symbol in enumerate(ALL_SYMBOLS):
        if symbol == SAFE:
            drift, vol = 0.0001, 0.001
        elif symbol == MARKET:
            drift, vol = 0.0006, 0.010
        elif symbol in FUNDS:
            drift, vol = 0.0002 + 0.0002 * (k % 3), 0.012
        else:
            drift, vol = 0.0004 + 0.0001 * (k % 4), 0.015 + 0.002 * (k % 3)
        rets = drift + vol * noise(rng, days)
        if symbol in (MARKET, *FUNDS):
            rets[MARKET_FALL[0] : MARKET_FALL[1]] -= 0.003
        if symbol in STOCKS:
            for day in range(60 + 7 * k, days - DIP_LENGTH, DIP_EVERY):
                rets[day : day + DIP_LENGTH] -= 2.2 * vol
        close = 100.0 * np.cumprod(1.0 + rets)
        prev = np.concatenate([[100.0], close[:-1]])
        open_ = prev * (1.0 + 0.3 * vol * noise(rng, days))
        high = np.maximum(open_, close) * (1.0 + 0.5 * vol * np.abs(noise(rng, days)))
        low = np.minimum(open_, close) * (1.0 - 0.5 * vol * np.abs(noise(rng, days)))
        frames[symbol] = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": 1e6},
            index=dates,
        )
    return Bars.from_frames(frames)


def tiny_bars() -> Bars:
    """A hand checkable market: one stock, six days, round numbers."""
    dates = pd.bdate_range("2024-01-01", periods=6)
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 104.0, 110.0, 108.0],
            "high": [101.0, 103.0, 105.0, 111.0, 112.0, 109.0],
            "low": [99.0, 100.0, 101.0, 103.0, 107.0, 105.0],
            "close": [100.0, 102.0, 104.0, 110.0, 108.0, 106.0],
            "volume": [1e6] * 6,
        },
        index=dates,
    )
    return Bars.from_frames({"AAA": frame})


def steady() -> Steady:
    params = SteadyParams(
        safe_asset=SAFE, market_symbol=MARKET, momentum_days=(20, 60, 120), top_n=2
    )
    return Steady(params, (*FUNDS, MARKET))


def swing() -> Swing:
    params = SwingParams(
        trend_fast_days=20,
        trend_slow_days=80,
        band_days=20,
        band_std=2.0,
        atr_days=14,
        stop_atr=3.0,
        max_hold_days=20,
        momentum_days=60,
    )
    return Swing(params, STOCKS)


def my_strategy() -> MyStrategy:
    params = MyStrategyParams(
        trend_days=60, breakout_days=30, exit_days=30, atr_days=14, stop_atr=2.0, max_hold_days=40
    )
    return MyStrategy(params, STOCKS)


def costs(bps: float = 5.0, commission: float = 0.0) -> CostModel:
    return CostModel(slippage_bps=bps, commission_per_order=commission)


def rules(max_positions: int = 4, max_position_pct: float = 0.5, risk: float = 0.01) -> SizingRules:
    return SizingRules(
        max_positions=max_positions,
        max_position_pct=max_position_pct,
        risk_per_trade=risk,
        cash_reserve_pct=0.02,
        min_order_dollars=10.0,
    )


STRATEGY_BUILDERS = {"steady": steady, "swing": swing, "my_strategy": my_strategy}
STRATEGY_RULES = {
    "steady": rules(max_positions=2, max_position_pct=1.0, risk=0.0),
    "swing": rules(max_positions=4, max_position_pct=0.3, risk=0.01),
    "my_strategy": rules(max_positions=4, max_position_pct=0.3, risk=0.01),
}
