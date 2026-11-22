"""Read ``config.toml`` into typed settings.

Why one settings file? So there are no hidden numbers in the code. When you
want to know what the bot does, you read one file. When you change a number,
you change it in one place, and the backtest and the bot both see the change.

Python 3.11 and newer can read TOML files with the built in ``tomllib`` module,
so this needs no extra package.
"""

from __future__ import annotations

import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, fields
from pathlib import Path
from typing import Any, get_type_hints

BPS_PER_UNIT = 10_000.0
"""Basis points in 1.0. 5 basis points = 5 / 10,000 = 0.05%."""

DEFAULT_CONFIG_NAME = "config.toml"
STATE_FILE_NAME = "bot_state.json"
MAX_PREFIX_LENGTH = 8  # keeps order ids short (brokers limit their length)
# Strategy names become part of every order id (hsw-swing-20261120-AAPL-buy),
# and a "-" would split the name in two. So: plain letters, digits and _.
NAME_PATTERN = re.compile(r"[A-Za-z0-9_]+")
PREFIX_PATTERN = re.compile(r"[A-Za-z0-9]+")

# Settings that every strategy section has. The rest of a section belongs to
# the strategy itself and is read by its own ``Params`` class.
SIZING_KEYS = ("max_positions", "max_position_pct", "risk_per_trade")


class ConfigError(Exception):
    """A setting is missing or wrong. The message always says how to fix it."""


@dataclass(frozen=True)
class CostModel:
    """What one trade costs us.

    ``slippage_bps`` is charged on every buy and every sell (one "side").
    Buys fill a little above the open price and sells a little below it.
    """

    slippage_bps: float
    commission_per_order: float
    stress_multiplier: float = 2.0

    def __post_init__(self) -> None:
        _check(self.slippage_bps >= 0, "slippage_bps must be 0 or more")
        _check(self.commission_per_order >= 0, "commission_per_order must be 0 or more")
        _check(self.stress_multiplier >= 1, "stress_multiplier must be 1 or more")

    @property
    def slippage_fraction(self) -> float:
        """Slippage as a plain fraction, for example 5 bps -> 0.0005."""
        return self.slippage_bps / BPS_PER_UNIT

    def scaled(self, multiplier: float) -> CostModel:
        """The same costs, multiplied. The stress test uses ``scaled(2.0)``."""
        return CostModel(
            slippage_bps=self.slippage_bps * multiplier,
            commission_per_order=self.commission_per_order * multiplier,
            stress_multiplier=self.stress_multiplier,
        )


@dataclass(frozen=True)
class SizingRules:
    """How big each new position may be.

    Three limits apply at once and the smallest one wins:

    * ``risk_per_trade``: the share of the account we accept to lose if the
      stop is hit. 0.01 means about 1%. 0 turns this limit off.
    * ``max_position_pct``: the most of the account one position may use.
    * the cash we actually have, minus a small reserve.

    ``min_fill_pct``: if the cash covers less than this share of the planned
    size, the position is not opened yet. It waits for the cash (which arrives
    from sells on the next run) instead of starting as a tiny leftover.
    """

    max_positions: int
    max_position_pct: float
    risk_per_trade: float
    cash_reserve_pct: float
    min_order_dollars: float
    min_fill_pct: float = 0.5

    def __post_init__(self) -> None:
        _check(self.max_positions >= 1, "max_positions must be at least 1")
        _check(0 < self.max_position_pct <= 1, "max_position_pct must be above 0 and at most 1")
        _check(0 <= self.risk_per_trade < 1, "risk_per_trade must be 0 or more and below 1")
        _check(0 <= self.cash_reserve_pct < 1, "sizing.cash_reserve_pct must be 0 or more, below 1")
        _check(self.min_order_dollars >= 1, "sizing.min_order_dollars must be at least 1")
        _check(0 < self.min_fill_pct <= 1, "sizing.min_fill_pct must be above 0 and at most 1")


@dataclass(frozen=True)
class StrategySettings:
    """One ``[strategies.NAME]`` section, split into the common parts and the rest."""

    name: str
    universe: tuple[str, ...]
    sizing: SizingRules
    params: Mapping[str, Any]


@dataclass(frozen=True)
class DataSettings:
    cache_dir: Path
    history_start: str
    benchmark: str
    cash_symbol: str


@dataclass(frozen=True)
class BacktestSettings:
    start: str
    starting_cash: float
    split_date: str
    random_entry_runs: int
    random_seed: int

    def __post_init__(self) -> None:
        _check(self.starting_cash > 0, "starting_cash must be above 0")
        _check(self.random_entry_runs >= 1, "random_entry_runs must be at least 1")
        _check(self.split_date > self.start, "split_date must come after start")


@dataclass(frozen=True)
class ReportSettings:
    strategies: tuple[str, ...]
    windows: Mapping[str, int]


@dataclass(frozen=True)
class BotSettings:
    """Settings for the hands off bot. See ``config.toml`` for what each one means."""

    sleeves: Mapping[str, float]
    history_days: int
    state_dir: Path
    journal_file: Path
    log_file: Path
    kill_switch_file: Path
    ignore_symbols: tuple[str, ...]
    daily_loss_limit_pct: float
    max_drawdown_pct: float
    minutes_before_open_cutoff: int
    max_bad_data_pct: float
    stale_data_days: int
    warn_after_failed_runs: int
    warn_after_failed_sells: int
    order_poll_seconds: float
    order_poll_interval: float
    request_timeout_seconds: float
    dry_run_cash: float
    client_order_prefix: str

    def __post_init__(self) -> None:
        _check(len(self.sleeves) >= 1, "bot.sleeves must name at least one strategy")
        for name, share in self.sleeves.items():
            _check(0 <= share <= 1, f"bot.sleeves.{name} must be 0 or more and at most 1")
            check_name(name, f"bot.sleeves.{name}")
        total = math.fsum(self.sleeves.values())
        _check(
            total <= 1 + 1e-9, f"bot.sleeves add up to {total:.2f}; they must add up to 1 or less"
        )
        _check(
            0 < self.daily_loss_limit_pct < 1, "bot.daily_loss_limit_pct must be between 0 and 1"
        )
        _check(0 < self.max_drawdown_pct < 1, "bot.max_drawdown_pct must be between 0 and 1")
        _check(0 <= self.max_bad_data_pct < 1, "bot.max_bad_data_pct must be 0 or more, below 1")
        _check(self.history_days >= 1, "bot.history_days must be at least 1")
        _check(self.warn_after_failed_runs >= 1, "bot.warn_after_failed_runs must be at least 1")
        _check(self.warn_after_failed_sells >= 1, "bot.warn_after_failed_sells must be at least 1")
        _check(self.order_poll_interval > 0, "bot.order_poll_interval must be above 0")
        _check(self.request_timeout_seconds > 0, "bot.request_timeout_seconds must be above 0")
        _check(self.dry_run_cash > 0, "bot.dry_run_cash must be above 0")
        prefix = self.client_order_prefix
        _check(
            PREFIX_PATTERN.fullmatch(prefix) is not None and len(prefix) <= MAX_PREFIX_LENGTH,
            f"bot.client_order_prefix must be 1 to {MAX_PREFIX_LENGTH} letters (a to z) or digits",
        )

    def state_path(self, mode: str) -> Path:
        """Each mode (paper, live) keeps its own records, so they can never mix."""
        return self.state_dir / mode / STATE_FILE_NAME


@dataclass(frozen=True)
class Config:
    """Every setting, typed and checked."""

    root: Path
    data: DataSettings
    backtest: BacktestSettings
    costs: CostModel
    report: ReportSettings
    universes: Mapping[str, tuple[str, ...]]
    strategies: Mapping[str, StrategySettings]
    bot: BotSettings

    def strategy(self, name: str) -> StrategySettings:
        """Settings for one strategy, with a helpful error if the name is unknown."""
        if name not in self.strategies:
            known = ", ".join(sorted(self.strategies))
            raise ConfigError(f"No [strategies.{name}] section in config.toml. Known: {known}.")
        return self.strategies[name]


# ----------------------------------------------------------------------------- loading


def find_config(path: str | Path | None = None) -> Path:
    """Find config.toml: the given path, else the current folder, else the repo folder."""
    if path is not None:
        candidate = Path(path)
        if not candidate.is_file():
            raise ConfigError(f"Config file {candidate} does not exist. Check the --config path.")
        return candidate
    here = Path.cwd() / DEFAULT_CONFIG_NAME
    if here.is_file():
        return here
    repo = Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_NAME
    if repo.is_file():
        return repo
    raise ConfigError(
        "Could not find config.toml. Run the command from the repo folder, "
        "or pass --config path/to/config.toml."
    )


def load_config(path: str | Path | None = None) -> Config:
    """Read and check config.toml. Raises ConfigError with a fix it hint on any problem."""
    config_path = find_config(path)
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"{config_path} is not valid TOML: {err}") from err
    root = config_path.resolve().parent
    universes = _read_universes(_table(raw, "universes"))
    sizing_shared = _table(raw, "sizing")
    strategy_tables = _table(raw, "strategies")
    for name in strategy_tables:
        check_name(name, f"[strategies.{name}]")
    strategies = {
        name: _read_strategy(name, _as_table(body, f"strategies.{name}"), universes, sizing_shared)
        for name, body in strategy_tables.items()
    }
    data = _table(raw, "data")
    report = _table(raw, "report")
    config = Config(
        root=root,
        data=DataSettings(
            cache_dir=root / _get(data, "cache_dir", str, "data"),
            history_start=_get(data, "history_start", str, "data"),
            benchmark=_get(data, "benchmark", str, "data"),
            cash_symbol=_get(data, "cash_symbol", str, "data"),
        ),
        backtest=parse_params(BacktestSettings, _table(raw, "backtest"), "backtest"),
        costs=parse_params(CostModel, _table(raw, "costs"), "costs"),
        report=ReportSettings(
            strategies=tuple(_get(report, "strategies", list, "report")),
            windows=_read_windows(_get(report, "windows", dict, "report")),
        ),
        universes=universes,
        strategies=strategies,
        bot=_read_bot(_table(raw, "bot"), root),
    )
    for name in (*config.report.strategies, *config.bot.sleeves):
        config.strategy(name)  # raises a clear error if a name has no section
    return config


def parse_params[T](cls: type[T], raw: Mapping[str, Any], section: str) -> T:
    """Build a settings dataclass from one TOML table, checking every key and type.

    Unknown keys are an error on purpose. A typo like ``stop_atrr`` would
    otherwise be ignored silently, and you would trade with a setting you did
    not mean.
    """
    hints = get_type_hints(cls)
    names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    unknown = sorted(set(raw) - names)
    if unknown:
        allowed = ", ".join(sorted(names))
        raise ConfigError(f"[{section}] has unknown setting(s) {unknown}. Allowed: {allowed}.")
    values: dict[str, Any] = {}
    for field in fields(cls):  # type: ignore[arg-type]
        if field.name not in raw:
            if field.default is MISSING and field.default_factory is MISSING:
                raise ConfigError(f"[{section}] is missing '{field.name}'. Add it to config.toml.")
            continue
        values[field.name] = _coerce(raw[field.name], hints[field.name], f"{section}.{field.name}")
    try:
        return cls(**values)
    except ConfigError as err:
        raise ConfigError(f"[{section}] {err}") from err


# ----------------------------------------------------------------------------- helpers


def check_name(name: str, where: str) -> None:
    """Strategy names: plain letters (a to z), digits and _ only, because they go in order ids."""
    if NAME_PATTERN.fullmatch(name) is None:
        raise ConfigError(
            f"{where}: the strategy name {name!r} may only use the letters a to z, digits and _ "
            "(no spaces or dashes: the name goes into every order id). For example: my_idea"
        )


def _check(ok: bool, message: str) -> None:
    if not ok:
        raise ConfigError(message)


def _table(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    if key not in raw:
        raise ConfigError(f"config.toml has no [{key}] section. Copy it from the original file.")
    return _as_table(raw[key], key)


def _as_table(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table of settings.")
    return value


def _get[T](table: Mapping[str, Any], key: str, kind: type[T], section: str) -> T:
    if key not in table:
        raise ConfigError(f"[{section}] is missing '{key}'. Add it to config.toml.")
    value = table[key]
    if not isinstance(value, kind):
        raise ConfigError(f"{section}.{key} should be a {kind.__name__}, not {value!r}.")
    return value


def _coerce(value: Any, hint: Any, where: str) -> Any:
    """Turn a TOML value into the type the dataclass expects, or explain why not."""
    if hint is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{where} should be a number, not {value!r}.")
        return float(value)
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} should be a whole number, not {value!r}.")
        return value
    if hint is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where} should be text in quotes, not {value!r}.")
        return value
    if hint is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where} should be true or false, not {value!r}.")
        return value
    if hint == tuple[int, ...]:
        if not isinstance(value, list) or not all(
            isinstance(v, int) and not isinstance(v, bool) for v in value
        ):
            raise ConfigError(f"{where} should be a list of whole numbers like [63, 126].")
        return tuple(value)
    if hint == tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f'{where} should be a list of text like ["SPY", "QQQ"].')
        return tuple(value)
    raise ConfigError(f"{where}: the settings reader does not know the type {hint}.")


def _read_universes(table: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    universes: dict[str, tuple[str, ...]] = {}
    for name, symbols in table.items():
        tickers = _coerce(symbols, tuple[str, ...], f"universes.{name}")
        if len(set(tickers)) != len(tickers):
            raise ConfigError(f"universes.{name} lists a symbol twice. Remove the copy.")
        universes[name] = tuple(s.strip().upper() for s in tickers)
    return universes


def _read_strategy(
    name: str,
    body: Mapping[str, Any],
    universes: Mapping[str, tuple[str, ...]],
    sizing_shared: Mapping[str, Any],
) -> StrategySettings:
    section = f"strategies.{name}"
    universe_name = _get(body, "universe", str, section)
    if universe_name not in universes:
        known = ", ".join(sorted(universes))
        raise ConfigError(
            f"{section}.universe = {universe_name!r} is not in [universes] ({known})."
        )
    sizing_raw = {key: body[key] for key in SIZING_KEYS if key in body}
    sizing_raw.update(sizing_shared)
    sizing = parse_params(SizingRules, sizing_raw, section)
    params = {k: v for k, v in body.items() if k not in (*SIZING_KEYS, "universe")}
    return StrategySettings(name, universes[universe_name], sizing, params)


def _read_windows(raw: Mapping[str, Any]) -> dict[str, int]:
    windows: dict[str, int] = {}
    for label, days in raw.items():
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise ConfigError(f"report.windows.{label} should be a number of trading days.")
        windows[label] = days
    return windows


def _read_bot(raw: Mapping[str, Any], root: Path) -> BotSettings:
    table = dict(raw)
    sleeves = table.pop("sleeves", None)
    if not isinstance(sleeves, dict):
        raise ConfigError("[bot] needs a line like: sleeves = { steady = 0.5, swing = 0.5 }")
    plain = parse_params(_BotRaw, table, "bot")
    shares = {name: float(_coerce(v, float, f"bot.sleeves.{name}")) for name, v in sleeves.items()}
    return BotSettings(
        sleeves=shares,
        history_days=plain.history_days,
        state_dir=root / plain.state_dir,
        journal_file=root / plain.journal_file,
        log_file=root / plain.log_file,
        kill_switch_file=root / plain.kill_switch_file,
        ignore_symbols=tuple(s.strip().upper() for s in plain.ignore_symbols),
        daily_loss_limit_pct=plain.daily_loss_limit_pct,
        max_drawdown_pct=plain.max_drawdown_pct,
        minutes_before_open_cutoff=plain.minutes_before_open_cutoff,
        max_bad_data_pct=plain.max_bad_data_pct,
        stale_data_days=plain.stale_data_days,
        warn_after_failed_runs=plain.warn_after_failed_runs,
        warn_after_failed_sells=plain.warn_after_failed_sells,
        order_poll_seconds=plain.order_poll_seconds,
        order_poll_interval=plain.order_poll_interval,
        request_timeout_seconds=plain.request_timeout_seconds,
        dry_run_cash=plain.dry_run_cash,
        client_order_prefix=plain.client_order_prefix,
    )


@dataclass(frozen=True)
class _BotRaw:
    """The plain [bot] values, before paths become full paths."""

    history_days: int
    state_dir: str
    journal_file: str
    log_file: str
    kill_switch_file: str
    ignore_symbols: tuple[str, ...]
    daily_loss_limit_pct: float
    max_drawdown_pct: float
    minutes_before_open_cutoff: int
    max_bad_data_pct: float
    stale_data_days: int
    warn_after_failed_runs: int
    warn_after_failed_sells: int
    order_poll_seconds: float
    order_poll_interval: float
    request_timeout_seconds: float
    dry_run_cash: float
    client_order_prefix: str
