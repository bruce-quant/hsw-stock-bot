"""Alpaca keys and the paper/live switch.

Keys come ONLY from environment variables, or from a ``.env`` file in the repo
folder (git ignores it, so it never gets uploaded). They are never printed,
never logged and never written anywhere else.

Live trading needs TWO separate switches, both on purpose:

1. ``ALPACA_PAPER=false`` in your environment or ``.env``, and
2. the ``--live`` flag on the command line.

With only one of them, the bot refuses to start. A single typo can never move
real money.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hsw_bot.config import ConfigError
from hsw_bot.runner import Mode

KEY_NAME = "ALPACA_API_KEY"
SECRET_NAME = "ALPACA_SECRET_KEY"
PAPER_NAME = "ALPACA_PAPER"
TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
FALSE_WORDS = frozenset({"false", "0", "no", "off"})


@dataclass(frozen=True)
class AlpacaKeys:
    api_key: str
    secret_key: str

    def __repr__(self) -> str:  # never show the keys, even by accident
        return "AlpacaKeys(api_key=***, secret_key=***)"


def read_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines from a .env file. Blank lines and # comments are skipped."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, _, value = text.partition("=")
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def merged_env(env_file: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The .env file's values, overridden by real environment variables."""
    merged = read_env_file(env_file)
    merged.update({k: v for k, v in (environ or os.environ).items() if k.startswith("ALPACA_")})
    return merged


def resolve_mode(env: Mapping[str, str], live_flag: bool) -> Mode:
    """Paper unless BOTH live switches are on. Refuse if only one is on."""
    raw = env.get(PAPER_NAME, "true").strip().lower()
    if raw in TRUE_WORDS:
        paper = True
    elif raw in FALSE_WORDS:
        paper = False
    else:
        raise ConfigError(f"{PAPER_NAME} must be true or false (it is {raw!r}).")
    if live_flag and paper:
        raise ConfigError(
            "You passed --live, but ALPACA_PAPER is not set to false. Live trading needs both "
            "switches. Nothing was sent. If you meant paper trading, drop --live."
        )
    if not live_flag and not paper:
        raise ConfigError(
            "ALPACA_PAPER is false, but you did not pass --live. Live trading needs both "
            "switches. Nothing was sent. For paper trading, set ALPACA_PAPER=true."
        )
    return Mode.LIVE if live_flag else Mode.PAPER


def read_keys(env: Mapping[str, str]) -> AlpacaKeys:
    """The two keys, or a clear message saying where to put them."""
    key, secret = env.get(KEY_NAME, "").strip(), env.get(SECRET_NAME, "").strip()
    missing = [name for name, value in ((KEY_NAME, key), (SECRET_NAME, secret)) if not value]
    if missing:
        raise ConfigError(
            f"Missing {' and '.join(missing)}. Copy .env.example to .env and fill in your "
            "Alpaca paper keys (docs/SETUP_ALPACA.md shows where to find them). "
            "Or try `hsw-bot dry-run`, which needs no keys."
        )
    return AlpacaKeys(key, secret)
