"""Rebuild every number, table and chart in README.md and docs/RESULTS.md.

Nothing in the results is typed by hand. ``hsw-bot report`` runs every test
again and writes the tables between marker lines in the docs, like this:

    <!-- report:summary:start -->
    ...generated table...
    <!-- report:summary:end -->

So when the data moves on, one command brings every page up to date, and a
number in the docs can never quietly disagree with the code.
"""

from __future__ import annotations

import logging
import platform
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pandas as pd

from hsw_bot import charts, stats
from hsw_bot.config import Config, CostModel
from hsw_bot.data import Bars, cache_info, load_history
from hsw_bot.engine import BacktestResult, Sleeve, run_backtest, run_sleeves
from hsw_bot.stats import Metrics, RandomEntryResult
from hsw_bot.strategies import build_strategy

log = logging.getLogger("hsw_bot")

SIGNIFICANCE = 0.05  # the usual line for "unlikely to be luck"
COLUMN_WIDTH = 20  # terminal table column width
FULL_YEAR_DAYS = 240  # a calendar year with fewer trading days in the test is marked "(part)"
IMAGE_DIR = Path("docs/images")
LIBRARIES = ("pandas", "numpy", "yfinance", "matplotlib", "alpaca-py")
BOT = "bot"  # the study name of the bot as configured
NA = "n/a"
# Sharpe ratios closer than this are called "about the same". Over a 20 year test the
# measurement error of a Sharpe ratio is about 0.2, so a smaller gap proves nothing.
SIMILAR_SHARPE = 0.1


@dataclass
class Study:
    """Everything we measured about one strategy (or the bot as configured)."""

    name: str
    title: str
    base: BacktestResult  # normal costs
    stressed: BacktestResult  # costs times the stress multiplier
    spy: pd.Series  # SPY buy and hold over the same days
    same_list: pd.Series  # equal weight of the strategy's own list
    cash: pd.Series  # the cash fund (SHY) bought and held
    random_test: RandomEntryResult | None
    random_note: str
    start_cash: float

    @property
    def label(self) -> str:
        """The name at the start of a sentence."""
        return "The bot as configured" if self.name == BOT else self.name.title()

    @property
    def short(self) -> str:
        """A short name for chart labels."""
        return "Bot" if self.name == BOT else self.name.title()

    @property
    def inline(self) -> str:
        """The name in the middle of a sentence."""
        return "the bot as configured" if self.name == BOT else self.name.title()


# ----------------------------------------------------------------------------- running the tests


def load_bars_for(config: Config, names: Sequence[str], *, refresh: bool = False) -> Bars:
    """History for these strategies plus the benchmark and cash fund, from the local copy."""
    symbols = {config.data.benchmark, config.data.cash_symbol}
    for name in {*names, *config.bot.sleeves}:
        symbols |= set(build_strategy(config, name).symbols())
    return load_history(
        sorted(symbols), config.data.history_start, config.data.cache_dir, refresh=refresh
    )


def study(
    config: Config,
    name: str,
    bars: Bars,
    *,
    cost_mult: float = 1.0,
    start: str | None = None,
    end: str | None = None,
    with_random_test: bool = True,
) -> Study:
    """Backtest one strategy alone, at normal and stressed costs, with every baseline."""
    strategy = build_strategy(config, name)
    rules = config.strategy(name).sizing
    cash = config.backtest.starting_cash
    first = start or config.backtest.start
    costs = config.costs.scaled(cost_mult)
    base = run_backtest(strategy, bars, costs, rules, cash, start=first, end=end)
    stress = costs.scaled(config.costs.stress_multiplier)
    stressed = run_backtest(strategy, bars, stress, rules, cash, start=first, end=end)
    days = pd.DatetimeIndex(base.equity.index)
    same = stats.equal_weight(bars, strategy.universe, costs, cash, days[0], days[-1])
    random_test = None
    if with_random_test:
        random_test = stats.random_entry_test(
            base,
            bars,
            strategy.tradable(),
            costs,
            config.backtest.random_entry_runs,
            config.backtest.random_seed,
        )
    return Study(
        name=name,
        title=strategy.title,
        base=base,
        stressed=stressed,
        spy=_baseline(bars, config.data.benchmark, costs, cash, days),
        same_list=same.reindex(days).ffill(),
        cash=_baseline(bars, config.data.cash_symbol, costs, cash, days),
        random_test=random_test,
        random_note=strategy.random_test_note,
        start_cash=cash,
    )


def bot_study(config: Config, bars: Bars, *, cost_mult: float = 1.0) -> Study:
    """Backtest the bot exactly as config.toml sets it up: every sleeve, in one account.

    It runs through the same sleeve sizing as the live bot (``planner.plan_sleeves``).
    """
    cash = config.backtest.starting_cash
    costs = config.costs.scaled(cost_mult)
    sleeves = [
        Sleeve(name, build_strategy(config, name), config.strategy(name).sizing, share)
        for name, share in config.bot.sleeves.items()
    ]
    start = config.backtest.start
    base = run_sleeves(sleeves, bars, costs, cash, start=start)
    stress = costs.scaled(config.costs.stress_multiplier)
    stressed = run_sleeves(sleeves, bars, stress, cash, start=start)
    days = pd.DatetimeIndex(base.equity.index)
    lists = [
        (stats.equal_weight(bars, s.strategy.universe, costs, cash, days[0]), s.share)
        for s in sleeves
    ]
    shares = ", ".join(f"{s.name} {s.share:.0%}" for s in sleeves)
    return Study(
        name=BOT,
        title=f"The bot as configured ({shares})",
        base=base,
        stressed=stressed,
        spy=_baseline(bars, config.data.benchmark, costs, cash, days),
        same_list=stats.blend(lists, cash).reindex(days).ffill(),
        cash=_baseline(bars, config.data.cash_symbol, costs, cash, days),
        random_test=None,
        random_note="",
        start_cash=cash,
    )


def _baseline(
    bars: Bars, symbol: str, costs: CostModel, cash: float, days: pd.DatetimeIndex
) -> pd.Series:
    curve = stats.buy_and_hold(bars, symbol, costs, cash, days[0], days[-1])
    return curve.reindex(days).ffill()


def print_scorecard(s: Study) -> None:
    """A short plain text summary for the terminal."""
    base_bps, stress_bps = s.base.costs.slippage_bps, s.stressed.costs.slippage_bps
    m1, m2 = stats.result_metrics(s.base), stats.result_metrics(s.stressed)
    scored = (m1, m2, *(stats.curve_metrics(c, s.start_cash) for c in (s.spy, s.same_list, s.cash)))
    rows: list[list[str]] = [
        [
            "",
            f"{s.name} {base_bps:g} bps",
            f"{s.name} {stress_bps:g} bps",
            "SPY",
            "same list",
            "cash (SHY)",
        ],
        ["yearly growth", *(pct(m.cagr) for m in scored)],
        ["worst drop", *(pct(m.max_drawdown) for m in scored)],
        ["sharpe", *(num(m.sharpe) for m in scored)],
        ["trades", str(m1.trades), str(m2.trades), "1", "-", "1"],
        ["win rate", pct(m1.win_rate), pct(m2.win_rate), "-", "-", "-"],
    ]
    print(f"\n{s.title}  ({m1.start.date()} to {m1.end.date()})")
    for row in rows:
        print("  " + "".join(cell.ljust(COLUMN_WIDTH) for cell in row))


# ----------------------------------------------------------------------------- formatting


def pct(value: float | None, digits: int = 1, sign: bool = False) -> str:
    """0.0768 -> '7.7%'. None -> 'n/a'."""
    if value is None:
        return NA
    return f"{value:+.{digits}%}" if sign else f"{value:.{digits}%}"


def num(value: float | None, digits: int = 2) -> str:
    return NA if value is None else f"{value:.{digits}f}"


def md_table(header: Sequence[str], rows: Sequence[Sequence[str]], align: str = "") -> str:
    """A Markdown table. ``align`` has one letter per column: l (left) or r (right)."""
    align = align or "l" + "r" * (len(header) - 1)
    marks = [":---" if a == "l" else "---:" for a in align]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(marks) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def span(m: Metrics) -> str:
    return f"{m.start:%Y-%m-%d} to {m.end:%Y-%m-%d}"


def _whole(
    curve: pd.Series,
    base: float,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> Metrics:
    part, before = stats.slice_curve(curve, base, start, end)
    return stats.curve_metrics(part, before)


# ----------------------------------------------------------------------------- tables


def full_table(s: Study) -> str:
    """Every headline number for one study, at both cost levels and against every baseline."""
    m1, m2 = stats.result_metrics(s.base), stats.result_metrics(s.stressed)
    spy, same, cash = (stats.curve_metrics(c, s.start_cash) for c in (s.spy, s.same_list, s.cash))
    mult = s.stressed.costs.slippage_bps / max(s.base.costs.slippage_bps, 1e-12)
    header = [
        "Measure",
        f"{s.label}, normal costs",
        f"{s.label}, {mult:g}x costs",
        "SPY buy and hold",
        "Same list, equal weight",
        "Cash (SHY)",
    ]
    rows = [
        ["Yearly growth (CAGR)", *(pct(m.cagr) for m in (m1, m2, spy, same, cash))],
        ["Total return", *(pct(m.total_return, 0) for m in (m1, m2, spy, same, cash))],
        ["Worst drop (max drawdown)", *(pct(m.max_drawdown) for m in (m1, m2, spy, same, cash))],
        ["Sharpe ratio", *(num(m.sharpe) for m in (m1, m2, spy, same)), NA],
        ["Yearly volatility", *(pct(m.volatility) for m in (m1, m2, spy, same, cash))],
        ["Trades", str(m1.trades), str(m2.trades), "1", "monthly top ups", "1"],
        ["Win rate", pct(m1.win_rate), pct(m2.win_rate), NA, NA, NA],
        ["Trades per month", num(m1.trades_per_month, 1), num(m2.trades_per_month, 1), NA, NA, NA],
        [
            "Average hold (trading days)",
            num(m1.avg_hold_days, 1),
            num(m2.avg_hold_days, 1),
            NA,
            NA,
            NA,
        ],
        ["Time in the market", pct(m1.exposure, 0), pct(m2.exposure, 0), "100%", "100%", "100%"],
    ]
    intro = f"Full history, {span(m1)}, starting with ${s.start_cash:,.0f}."
    return intro + "\n\n" + md_table(header, rows)


def window_rows(s: Study, windows: dict[str, int]) -> list[list[str]]:
    """Returns over recent windows, carved out of the one continuous backtest.

    Columns: label, return, return at 2x costs, worst drop, SPY return,
    SPY worst drop, same list return, cash return.
    """
    rows = []
    items: list[tuple[str, int | None]] = [*windows.items(), ("Full history", None)]
    for label, days in items:

        def cut(curve: pd.Series, days: int | None = days) -> Metrics:
            if days is None:
                return stats.curve_metrics(curve, s.start_cash)
            part, before = stats.last_days(curve, s.start_cash, days)
            return stats.curve_metrics(part, before)

        m1, m2 = cut(s.base.equity), cut(s.stressed.equity)
        spy, same, cash = cut(s.spy), cut(s.same_list), cut(s.cash)
        rows.append(
            [
                f"{label} ({span(m1)})",
                pct(m1.total_return, sign=True),
                pct(m2.total_return, sign=True),
                pct(m1.max_drawdown),
                pct(spy.total_return, sign=True),
                pct(spy.max_drawdown),
                pct(same.total_return, sign=True),
                pct(cash.total_return, sign=True),
            ]
        )
    return rows


def window_table(s: Study, windows: dict[str, int]) -> str:
    header = [
        "Window",
        "Return, normal costs",
        "Return, 2x costs",
        "Worst drop",
        "SPY return",
        "SPY worst drop",
        "Same list return",
        "Cash (SHY) return",
    ]
    return md_table(header, window_rows(s, windows))


def split_table(s: Study, split: str) -> str:
    """In sample (before the split date) and out of sample (from it on)."""
    header = [
        "Period",
        "Yearly growth",
        "With 2x costs",
        "Worst drop",
        "Sharpe",
        "SPY yearly growth",
        "SPY worst drop",
        "Same list yearly growth",
        "Cash (SHY) yearly growth",
    ]
    rows = []
    cut_day = pd.Timestamp(split)
    for label, start, end in (
        ("In sample", None, cut_day - pd.Timedelta(days=1)),
        ("Out of sample", cut_day, None),
    ):
        m1 = stats.result_metrics(s.base, start, end)
        m2 = stats.result_metrics(s.stressed, start, end)
        spy = _whole(s.spy, s.start_cash, start, end)
        same = _whole(s.same_list, s.start_cash, start, end)
        cash = _whole(s.cash, s.start_cash, start, end)
        rows.append(
            [
                f"{label} ({span(m1)})",
                pct(m1.cagr),
                pct(m2.cagr),
                pct(m1.max_drawdown),
                num(m1.sharpe),
                pct(spy.cagr),
                pct(spy.max_drawdown),
                pct(same.cagr),
                pct(cash.cagr),
            ]
        )
    return md_table(header, rows)


def random_table(s: Study) -> str:
    r = s.random_test
    if r is None:
        return "Not enough closed trades for the random entry test."
    header = [
        "Trades",
        "Strategy: average per trade",
        "Random traders: average",
        "Random traders: middle 90%",
        "p value",
    ]
    rows = [
        [
            str(r.trades),
            pct(r.strategy_mean, 2, sign=True),
            pct(r.random_mean, 2, sign=True),
            f"{pct(r.random_low, 2, sign=True)} to {pct(r.random_high, 2, sign=True)}",
            f"{r.p_value:.3f}",
        ]
    ]
    note = (
        f"{r.runs:,} random traders. Each made the same {r.trades:,} trades with the same "
        "holding lengths, but on random stocks from the same list and random days."
    )
    if s.random_note:
        note += " " + s.random_note
    return md_table(header, rows) + "\n\n" + note


def years_table(s: Study) -> str:
    strategy = stats.period_returns(s.base.equity, s.start_cash, "YE")
    stressed = stats.period_returns(s.stressed.equity, s.start_cash, "YE")
    spy = stats.period_returns(s.spy, s.start_cash, "YE")
    same = stats.period_returns(s.same_list, s.start_cash, "YE")
    cash = stats.period_returns(s.cash, s.start_cash, "YE")
    header = ["Year", "Strategy", "With 2x costs", "SPY", "Same list", "Cash (SHY)", "Beat SPY?"]
    rows = []
    days_per_year = pd.Series(pd.DatetimeIndex(s.base.equity.index).year).value_counts()
    for ts, value in zip(pd.DatetimeIndex(strategy.index), strategy.to_numpy(), strict=True):
        partial = " (part)" if days_per_year.get(ts.year, 0) < FULL_YEAR_DAYS else ""
        rows.append(
            [
                f"{ts.year}{partial}",
                pct(value, sign=True),
                pct(stressed.get(ts), sign=True),
                pct(spy.get(ts), sign=True),
                pct(same.get(ts), sign=True),
                pct(cash.get(ts), sign=True),
                "yes" if value > spy.get(ts, float("nan")) else "no",
            ]
        )
    wins = sum(r[-1] == "yes" for r in rows)
    return md_table(header, rows) + f"\n\nBeat SPY in {wins} of {len(rows)} calendar years."


def drawdown_table(s: Study) -> str:
    rows = []
    for d in stats.worst_drawdowns(s.base.equity, s.start_cash):
        back = f"{d.recovered:%Y-%m-%d}" if d.recovered is not None else "not yet"
        rows.append([f"{d.peak:%Y-%m-%d}", f"{d.trough:%Y-%m-%d}", back, pct(d.depth)])
    return md_table(["High point", "Low point", "Back to the high", "Drop"], rows, "llll")


def summary_table(studies: Sequence[Study], split: str) -> str:
    """The README headline: one column per strategy, plus SPY and cash."""
    first = studies[0]
    spy = stats.curve_metrics(first.spy, first.start_cash)
    cash = stats.curve_metrics(first.cash, first.start_cash)
    spy_oos = _whole(first.spy, first.start_cash, pd.Timestamp(split))
    cash_oos = _whole(first.cash, first.start_cash, pd.Timestamp(split))
    scores = [(stats.result_metrics(s.base), stats.result_metrics(s.stressed)) for s in studies]
    oos = [stats.result_metrics(s.base, pd.Timestamp(split)) for s in studies]
    lists = [stats.curve_metrics(s.same_list, s.start_cash) for s in studies]
    header = ["Measure", *(s.label for s in studies), "SPY buy and hold", "Cash (SHY)"]
    rows = [
        [
            "Yearly growth, normal costs",
            *(pct(m1.cagr) for m1, _ in scores),
            pct(spy.cagr),
            pct(cash.cagr),
        ],
        ["Yearly growth, 2x costs", *(pct(m2.cagr) for _, m2 in scores), NA, NA],
        [
            "Worst drop",
            *(pct(m1.max_drawdown) for m1, _ in scores),
            pct(spy.max_drawdown),
            pct(cash.max_drawdown),
        ],
        ["Sharpe ratio", *(num(m1.sharpe) for m1, _ in scores), num(spy.sharpe), NA],
        [
            f"Yearly growth from {split[:4]} (out of sample)",
            *(pct(m.cagr) for m in oos),
            pct(spy_oos.cagr),
            pct(cash_oos.cagr),
        ],
        ["Its own list, equal weight: yearly growth", *(pct(m.cagr) for m in lists), NA, NA],
        [
            "Random entry test, p value",
            *(f"{s.random_test.p_value:.2f}" if s.random_test else NA for s in studies),
            NA,
            NA,
        ],
    ]
    m1 = scores[0][0]
    return (
        md_table(header, rows) + f"\n\nTested from {m1.start:%Y-%m-%d} to {m1.end:%Y-%m-%d} with "
        f"${first.start_cash:,.0f}. Out of sample = from {split}."
    )


def recent_table(studies: Sequence[Study], windows: dict[str, int]) -> str:
    """Total returns over the recent windows: every strategy, its list, SPY and cash."""
    labels = [*windows, "Full history"]
    rows: list[list[str]] = []
    first = studies[0]
    for s in studies:
        cells = window_rows(s, windows)
        rows.append([f"{s.label}, normal costs", *[c[1] for c in cells]])
        rows.append([f"{s.label}, 2x costs", *[c[2] for c in cells]])
    for s in studies:
        if s.name != BOT:
            cells = window_rows(s, windows)
            rows.append([f"{s.label} list, equal weight", *[c[6] for c in cells]])
    cells = window_rows(first, windows)
    rows.append(["SPY buy and hold", *[c[4] for c in cells]])
    rows.append(["Cash (SHY)", *[c[7] for c in cells]])
    return md_table(["", *labels], rows)


# ----------------------------------------------------------------------------- plain words


def headline(studies: Sequence[Study]) -> str:
    """One honest sentence for the top of the README, written from the numbers."""
    beat = [s.label for s in studies if _beats_spy(s)]
    calmer = [s for s in studies if _calmer_than_spy(s)]
    if not beat:
        first = "In our backtest, no strategy beat simply holding SPY."
    else:
        first = f"In our backtest, {' and '.join(beat)} beat simply holding SPY."
    second = ""
    if calmer and len(calmer) == len(studies):
        second = " However every strategy fell much less than SPY in the bad years."
    return f"**{first}{second}**"


def _beats_spy(s: Study) -> bool:
    mine = stats.result_metrics(s.base).cagr or 0.0
    spy = stats.curve_metrics(s.spy, s.start_cash).cagr or 0.0
    return mine > spy


def _calmer_than_spy(s: Study) -> bool:
    mine = stats.result_metrics(s.base).max_drawdown
    return mine > stats.curve_metrics(s.spy, s.start_cash).max_drawdown


def worst_stretch(s: Study) -> str:
    """The worst drop told as a short story, written from the data."""
    drops = stats.worst_drawdowns(s.base.equity, s.start_cash, count=1)
    if not drops:
        return f"{s.label} never fell below an earlier high."
    d = drops[0]
    text = (
        f"{s.label} had its worst drop from {d.peak:%Y-%m-%d} to {d.trough:%Y-%m-%d}: "
        f"{pct(d.depth)} from its high."
    )
    if d.recovered is None:
        return text + " It has not yet climbed back to that high."
    months = (d.recovered - d.peak).days / stats.DAYS_PER_YEAR * stats.MONTHS_PER_YEAR
    return text + (
        f" It was back at the old high on {d.recovered:%Y-%m-%d}, "
        f"about {months:.0f} months after the drop began."
    )


def sharpe_sentence(sharpe: float, spy_sharpe: float) -> str:
    """Compare the Sharpe ratios, and say plainly when the gap is too small to mean anything."""
    said = f"Its Sharpe ratio was {sharpe:.2f}, against {spy_sharpe:.2f} for SPY."
    gap = sharpe - spy_sharpe
    if abs(gap) < SIMILAR_SHARPE:
        return (
            f"{said} A gap that small is within the noise of the test. Therefore it earned "
            "about the same return for each unit of risk."
        )
    better = "more" if gap > 0 else "less"
    return f"{said} Therefore it earned {better} return for each unit of risk."


def verdict(s: Study, split: str, windows: dict[str, int]) -> str:
    """The honest summary, written from the numbers (so it can never go stale)."""
    name = s.label
    m1, m2 = stats.result_metrics(s.base), stats.result_metrics(s.stressed)
    spy, same, cash = (stats.curve_metrics(c, s.start_cash) for c in (s.spy, s.same_list, s.cash))
    oos = stats.result_metrics(s.base, pd.Timestamp(split))
    spy_oos = _whole(s.spy, s.start_cash, pd.Timestamp(split))
    lines = [
        f"{name} grew {pct(m1.cagr)} a year after normal costs. SPY grew {pct(spy.cagr)} a year."
    ]
    if (m1.cagr or 0) < (spy.cagr or 0):
        lines.append(f"Therefore {s.inline} did not beat simply holding SPY.")
    else:
        lines.append(f"Therefore {s.inline} beat simply holding SPY in this test.")
    drop = (
        f"its worst drop was {pct(m1.max_drawdown, 0)}, against {pct(spy.max_drawdown, 0)} for SPY."
    )
    lines.append(f"However {drop}" if m1.max_drawdown > spy.max_drawdown else drop.capitalize())
    lines.append(sharpe_sentence(m1.sharpe, spy.sharpe))
    own = (
        "the same lists, in the same shares,"
        if s.name == BOT
        else "an equal weight portfolio of its own list"
    )
    compare = "more" if (same.cagr or 0) > (m1.cagr or 0) else "less"
    lines.append(f"Holding {own} grew {pct(same.cagr)} a year, {compare} than {s.inline}.")
    beat_cash = (m1.cagr or 0) > (cash.cagr or 0)
    lines.append(
        f"Cash in SHY grew {pct(cash.cagr)} a year, so {s.inline} "
        f"{'did' if beat_cash else 'did not'} beat cash over the full test."
    )
    if windows:
        label, days = max(windows.items(), key=lambda item: item[1])
        recent, before = stats.last_days(s.base.equity, s.start_cash, days)
        cash_part, cash_before = stats.last_days(s.cash, s.start_cash, days)
        mine = stats.curve_metrics(recent, before).total_return
        safe = stats.curve_metrics(cash_part, cash_before).total_return
        lines.append(
            f"Over the {label.lower()}, it returned {pct(mine, sign=True)}, against "
            f"{pct(safe, sign=True)} for cash."
        )
    if (m2.cagr or 0) > 0:
        lines.append(f"With double costs it still grew {pct(m2.cagr)} a year.")
    else:
        lines.append("With double costs it lost money.")
    lines.append(
        f"From {split[:4]} on (out of sample) it grew {pct(oos.cagr)} a year, "
        f"against {pct(spy_oos.cagr)} for SPY."
    )
    r = s.random_test
    if r is not None:
        if r.p_value < SIGNIFICANCE:
            lines.append(
                f"The random entry test gave p = {r.p_value:.2f}. Therefore its trades were "
                "better than random trades of the same length, and luck alone is a poor "
                "explanation."
            )
        else:
            lines.append(
                f"The random entry test gave p = {r.p_value:.2f}. Therefore its trades were "
                "not clearly better than random trades of the same length."
            )
        if s.random_note:
            lines.append(s.random_note)
    return " ".join(lines)


# ----------------------------------------------------------------------------- writing the docs


def replace_block(text: str, name: str, content: str) -> str:
    """Put ``content`` between the report:NAME markers. Missing markers are an error."""
    pattern = re.compile(
        rf"(<!-- report:{re.escape(name)}:start -->\n)(.*?)(<!-- report:{re.escape(name)}:end -->)",
        re.DOTALL,
    )
    if not pattern.search(text):
        raise ValueError(f"Marker block {name!r} not found. Add the start and end markers first.")
    return pattern.sub(lambda m: m.group(1) + content.rstrip() + "\n" + m.group(3), text)


def fill_file(path: Path, blocks: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8")
    for name, content in blocks.items():
        text = replace_block(text, name, content)
    path.write_text(text, encoding="utf-8")


def library_versions() -> str:
    rows = [["Python", platform.python_version()]]
    for lib in LIBRARIES:
        try:
            rows.append([lib, version(lib)])
        except PackageNotFoundError:
            rows.append([lib, "not installed"])
    return md_table(["Library", "Version"], rows, "ll")


def data_note(config: Config, bars: Bars) -> str:
    info = cache_info(config.data.cache_dir)
    stamp = str(info.get("downloaded_at_utc", ""))
    when = stamp[:16].replace("T", " ") if stamp else "at an unknown time"
    listed = info.get("failed")
    failed = [str(f) for f in listed] if isinstance(listed, list) else []
    note = (
        f"Daily bars from Yahoo Finance (adjusted for splits and dividends), "
        f"{bars.dates[0]:%Y-%m-%d} to {bars.dates[-1]:%Y-%m-%d}. "
        f"Downloaded {when} UTC. Data end date: {bars.dates[-1]:%Y-%m-%d}."
    )
    if failed:
        note += f" No data for: {', '.join(failed)}."
    return note


def settings_snapshot(config: Config, names: Sequence[str]) -> str:
    """The exact settings used, copied from config.toml, so results can be reproduced."""
    rows = []
    for name in names:
        settings = config.strategy(name)
        for key, value in settings.params.items():
            rows.append([name, key, str(value)])
        sizing = settings.sizing
        rows.append([name, "max_positions", str(sizing.max_positions)])
        rows.append([name, "max_position_pct", str(sizing.max_position_pct)])
        rows.append([name, "risk_per_trade", str(sizing.risk_per_trade)])
    sizing = config.strategy(names[0]).sizing
    rows.append(["all", "cash_reserve_pct", f"{sizing.cash_reserve_pct:g}"])
    rows.append(["all", "min_fill_pct", f"{sizing.min_fill_pct:g}"])
    rows.append(["all", "min_order_dollars", f"{sizing.min_order_dollars:g}"])
    costs = config.costs
    rows.append(["all", "slippage_bps (per side)", f"{costs.slippage_bps:g}"])
    rows.append(["all", "commission_per_order", f"{costs.commission_per_order:g}"])
    rows.append(["all", "stress_multiplier", f"{costs.stress_multiplier:g}"])
    shares = ", ".join(f"{n} = {s:g}" for n, s in config.bot.sleeves.items())
    rows.append(["bot", "sleeves", shares])
    return md_table(["Strategy", "Setting", "Value"], rows, "lll")


def build_report(config: Config, *, refresh: bool = False) -> None:
    """Run everything and rewrite the generated parts of the docs and the charts."""
    names = list(config.report.strategies)
    bars = load_bars_for(config, names, refresh=refresh)
    split = config.backtest.split_date
    windows = dict(config.report.windows)
    studies = []
    for name in names:
        log.info(
            "Testing %s (normal costs, %gx costs, baselines, random entry test)...",
            name,
            config.costs.stress_multiplier,
        )
        studies.append(study(config, name, bars))
    log.info("Testing the bot as configured (all sleeves in one account)...")
    studies.append(bot_study(config, bars))
    images = config.root / IMAGE_DIR
    images.mkdir(parents=True, exist_ok=True)
    charts.banner(images / "banner.png")
    for s in studies:
        charts.tear_sheet(s, images / f"{s.name}_tearsheet.png")
    readme: dict[str, str] = {
        "headline": headline(studies),
        "summary": summary_table(studies, split),
        "recent": recent_table(studies, windows),
        "verdicts": "\n\n".join(f"**{s.title}.** {verdict(s, split, windows)}" for s in studies),
        "data": data_note(config, bars),
    }
    fill_file(config.root / "README.md", readme)
    results: dict[str, str] = {
        "data": data_note(config, bars),
        "versions": library_versions(),
        "settings": settings_snapshot(config, names),
    }
    builders: dict[str, Callable[[Study], str]] = {
        "full": full_table,
        "windows": lambda s: window_table(s, windows),
        "split": lambda s: split_table(s, split),
        "random": random_table,
        "years": years_table,
        "drawdowns": drawdown_table,
        "verdict": lambda s: verdict(s, split, windows),
        "stretch": worst_stretch,
    }
    for s in studies:
        for key, build in builders.items():
            if s.name == BOT and key == "random":
                continue
            results[f"{s.name}-{key}"] = build(s)
    fill_file(config.root / "docs" / "RESULTS.md", results)
    log.info(
        "Report written: README.md, docs/RESULTS.md and %d charts in %s.",
        len(studies) + 1,
        IMAGE_DIR,
    )
