"""All strategies the bot knows about.

To add your own: write it (copy ``my_strategy.py``), add the class to
``STRATEGIES`` below, and add a ``[strategies.NAME]`` section to config.toml.
The backtest, the report, the bot and the guard tests then all pick it up.
"""

from __future__ import annotations

from hsw_bot.config import Config, ConfigError
from hsw_bot.strategies.base import Day, Entry, Exit, LookaheadError, Position, Strategy
from hsw_bot.strategies.my_strategy import MyStrategy
from hsw_bot.strategies.steady import Steady
from hsw_bot.strategies.swing import Swing

STRATEGIES: dict[str, type[Strategy]] = {cls.name: cls for cls in (Steady, Swing, MyStrategy)}

__all__ = [
    "STRATEGIES",
    "Day",
    "Entry",
    "Exit",
    "LookaheadError",
    "Position",
    "Strategy",
    "build_strategy",
]


def build_strategy(config: Config, name: str) -> Strategy:
    """Create the strategy called ``name`` with its settings from config.toml."""
    if name not in STRATEGIES:
        known = ", ".join(sorted(STRATEGIES))
        raise ConfigError(
            f"Unknown strategy {name!r}. Known: {known}. "
            "New strategies must be added to STRATEGIES in hsw_bot/strategies/__init__.py."
        )
    settings = config.strategy(name)
    return STRATEGIES[name].from_settings(settings.params, settings.universe)
