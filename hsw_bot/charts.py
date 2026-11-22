"""Charts for the docs: one tear sheet per strategy, and the README banner.

One palette for everything (the HSW palette below). The three series colors
were checked with a color blindness simulator, and every chart also has a
legend and labels, so no reader has to tell lines apart by color alone.
Light background, thin lines, quiet grid: the data is the only loud thing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("Agg")  # draw to files, never open a window

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import FancyBboxPatch
from matplotlib.ticker import FuncFormatter

from hsw_bot import stats

if TYPE_CHECKING:
    from hsw_bot.report import Study


@dataclass(frozen=True)
class Palette:
    surface: str = "#fcfcfb"
    ink: str = "#0b0b0b"
    ink_soft: str = "#52514e"
    muted: str = "#898781"
    grid: str = "#e1e0d9"
    axis: str = "#c3c2b7"
    strategy: str = "#2a78d6"  # blue: the strategy
    strategy_light: str = "#86b6ef"  # the same strategy with double costs
    market: str = "#eb6834"  # orange: SPY buy and hold
    same_list: str = "#1baf7a"  # aqua: equal weight of the same list
    loss: str = "#e34948"
    middle: str = "#f0efec"


HSW = Palette()
DPI = 150
LINE = 1.6
THIN = 1.1
WASH = 0.10  # area fills are a light wash of the line color
LABEL_GAP = 0.06  # minimum gap between end labels, in log10 units of the y axis
HIST_CLIP = 0.25  # the trade histogram shows -25% to +25%; the rest is counted in a note
HIST_BINS = 50
THOUSAND, TEN_THOUSAND, MILLION = 1_000, 10_000, 1_000_000
BANNER_BARS = 66
MONTHS = ("J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D")
YEAR_COLUMN = 13.6  # x position (in month cells) of the yearly totals column
HALF_PERCENT = 0.005  # yearly totals this close to zero are shown as 0%


def _style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": HSW.surface,
            "axes.facecolor": HSW.surface,
            "savefig.facecolor": HSW.surface,
            "axes.edgecolor": HSW.axis,
            "axes.labelcolor": HSW.ink_soft,
            "axes.titlecolor": HSW.ink,
            "axes.titlesize": 12,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.grid": True,
            "grid.color": HSW.grid,
            "grid.linewidth": 0.6,
            "grid.linestyle": "-",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "xtick.color": HSW.muted,
            "ytick.color": HSW.muted,
            "xtick.labelcolor": HSW.ink_soft,
            "ytick.labelcolor": HSW.ink_soft,
            "legend.frameon": False,
            "legend.fontsize": 9,
            "font.size": 10,
            "font.family": "DejaVu Sans",
        }
    )


def _money(value: float, _pos: object = None) -> str:
    if value >= MILLION:
        return f"${value / MILLION:.1f}M"
    if value >= TEN_THOUSAND:
        return f"${value / THOUSAND:.0f}K"
    return f"${value:,.0f}"


def _percent(value: float, _pos: object = None) -> str:
    return f"{value:.0%}"


# ----------------------------------------------------------------------------- tear sheet


def tear_sheet(s: Study, path: Path) -> None:
    """Four views of one strategy: growth, drops, months, and single trades."""
    _style()
    fig = plt.figure(figsize=(12, 12.5), dpi=DPI)
    grid = fig.add_gridspec(
        3,
        2,
        height_ratios=[1.35, 0.75, 1.0],
        width_ratios=[1.5, 1.0],
        hspace=0.42,
        wspace=0.16,
        left=0.07,
        right=0.86,
        top=0.90,
        bottom=0.05,
    )
    m = stats.result_metrics(s.base)
    fig.text(
        0.07,
        0.965,
        f"HSW Stock Bot  |  {s.title}",
        fontsize=17,
        fontweight="bold",
        color=HSW.ink,
    )
    fig.text(
        0.07,
        0.94,
        f"Backtest {m.start:%Y-%m-%d} to {m.end:%Y-%m-%d}, ${s.start_cash:,.0f} start, "
        f"costs {s.base.costs.slippage_bps:g} bps per side (dashed line: "
        f"{s.stressed.costs.slippage_bps:g} bps). Past results do not predict the future.",
        fontsize=10,
        color=HSW.ink_soft,
    )
    _growth(fig.add_subplot(grid[0, :]), s)
    _drawdowns(fig.add_subplot(grid[1, :]), s)
    _months(fig.add_subplot(grid[2, 0]), s)
    _trades(fig.add_subplot(grid[2, 1]), s)
    fig.savefig(path, dpi=DPI)
    plt.close(fig)


def _growth(ax: Axes, s: Study) -> None:
    name = s.label
    lines = [
        (s.base.equity, f"{name}", HSW.strategy, "-", LINE, s.short),
        (s.stressed.equity, f"{name}, 2x costs", HSW.strategy_light, "--", THIN, "2x costs"),
        (s.spy, "SPY buy and hold", HSW.market, "-", THIN, "SPY"),
        (s.same_list, "Same list, equal weight", HSW.same_list, "-", THIN, "Same list"),
        (s.cash, "Cash (SHY)", HSW.muted, ":", THIN, "Cash"),
    ]
    for curve, label, color, style, width, _ in lines:
        ax.plot(
            curve.index,
            curve.to_numpy(),
            color=color,
            linestyle=style,
            linewidth=width,
            label=label,
            solid_capstyle="round",
        )
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(FuncFormatter(_money))
    ax.yaxis.set_minor_formatter(FuncFormatter(lambda v, p: ""))
    ax.set_title(f"Growth of ${s.start_cash:,.0f} (log scale)", pad=10)
    ax.legend(loc="upper left", ncols=2)
    _end_labels(ax, [(curve, color, short) for curve, _, color, _, _, short in lines])
    ax.margins(x=0.01)


def _end_labels(ax: Axes, series: list[tuple[pd.Series, str, str]]) -> None:
    """Final values at the right edge, spread apart so they never overlap.

    Each label sits just right of the plot and a thin leader line in the
    series color joins it to the end of its line.
    """
    ends = sorted(
        ((float(c.iloc[-1]), color, name) for c, color, name in series), key=lambda t: t[0]
    )
    heights: list[float] = []
    for value, _, _ in ends:
        y = math.log10(value)
        if heights and y - heights[-1] < LABEL_GAP:
            y = heights[-1] + LABEL_GAP
        heights.append(y)
    first, last = series[0][0].index[0], series[0][0].index[-1]
    x_text = last + (last - first) * 0.035
    for (value, color, name), y in zip(ends, heights, strict=True):
        ax.annotate(
            f"{name} {_money(value)}",
            xy=(last, value),
            xytext=(x_text, 10**y),
            va="center",
            fontsize=9,
            color=HSW.ink,
            annotation_clip=False,
            arrowprops={
                "arrowstyle": "-",
                "color": color,
                "linewidth": 0.8,
                "shrinkA": 1,
                "shrinkB": 1,
            },
        )


def _drawdowns(ax: Axes, s: Study) -> None:
    mine = stats.drawdown_curve(s.base.equity, s.start_cash)
    market = stats.drawdown_curve(s.spy, s.start_cash)
    ax.fill_between(mine.index, mine.to_numpy(), 0, color=HSW.strategy, alpha=WASH, linewidth=0)
    ax.plot(
        mine.index,
        mine.to_numpy(),
        color=HSW.strategy,
        linewidth=THIN,
        label=s.label,
    )
    ax.plot(
        market.index, market.to_numpy(), color=HSW.market, linewidth=THIN, label="SPY buy and hold"
    )
    worst = float(mine.min())
    when = mine.idxmin()
    ax.annotate(
        f"worst {worst:.0%}",
        xy=(when, worst),
        xytext=(8, -4),
        textcoords="offset points",
        fontsize=9,
        color=HSW.ink,
    )
    ax.yaxis.set_major_formatter(FuncFormatter(_percent))
    ax.set_title("Drops from the last high point (drawdown)", pad=10)
    ax.legend(loc="lower left", ncols=2)
    ax.margins(x=0.01)


def _months(ax: Axes, s: Study) -> None:
    table = stats.monthly_table(s.base.equity, s.start_cash)
    values = table.reindex(columns=range(1, 13)).to_numpy(dtype="float64")
    limit = max(float(np.nanpercentile(np.abs(values), 95)), 0.01)
    colors = LinearSegmentedColormap.from_list(
        "hsw_diverging", [HSW.loss, HSW.middle, HSW.strategy]
    )
    ax.imshow(np.ma.masked_invalid(values), cmap=colors, vmin=-limit, vmax=limit, aspect="auto")
    years = [str(y) for y in table.index]
    step = max(len(years) // 12, 1)
    ax.set_yticks(range(0, len(years), step), years[::step])
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(length=0)
    yearly = stats.period_returns(s.base.equity, s.start_cash, "YE")
    for row, value in enumerate(yearly.to_numpy()):
        label = "0%" if abs(value) < HALF_PERCENT else f"{value:+.0%}"
        ax.text(YEAR_COLUMN, row, label, va="center", ha="right", fontsize=7.5, color=HSW.ink_soft)
    ax.set_xlim(-0.5, YEAR_COLUMN + 0.1)
    ax.set_xticks([*range(12), YEAR_COLUMN - 0.6], [*MONTHS, "Year"])
    ax.set_title("Monthly returns (blue up, red down)", pad=10)


def _trades(ax: Axes, s: Study) -> None:
    returns = np.array([t.return_pct for t in s.base.trades])
    if returns.size == 0:
        ax.text(0.5, 0.5, "no closed trades", ha="center", transform=ax.transAxes)
        return
    shown = np.clip(returns, -HIST_CLIP, HIST_CLIP)
    outside = int(np.sum(np.abs(returns) > HIST_CLIP))
    ax.hist(
        shown,
        bins=HIST_BINS,
        range=(-HIST_CLIP, HIST_CLIP),
        color=HSW.strategy,
        alpha=0.85,
        rwidth=0.85,
    )
    ax.axvline(0, color=HSW.axis, linewidth=1)
    mean = float(returns.mean())
    ax.axvline(mean, color=HSW.ink, linewidth=1, linestyle="-")
    ax.annotate(
        f"average {mean:+.2%}",
        xy=(mean, 1),
        xycoords=("data", "axes fraction"),
        xytext=(4, -12),
        textcoords="offset points",
        fontsize=9,
        color=HSW.ink,
    )
    ax.xaxis.set_major_formatter(FuncFormatter(_percent))
    win = float(np.mean(returns > 0))
    ax.set_title(f"Single trades: {returns.size:,} trades, {win:.0%} winners", pad=10)
    note = f"{outside} trades beyond +/-{HIST_CLIP:.0%} are shown at the edges." if outside else ""
    ax.set_xlabel(f"Return per trade, after costs. {note}", fontsize=8.5)


# ----------------------------------------------------------------------------- banner


def banner(path: Path) -> None:
    """A simple branded banner for the top of the README."""
    _style()
    fig = plt.figure(figsize=(12, 3.0), dpi=DPI)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 3)
    ax.axis("off")
    ax.add_patch(
        FancyBboxPatch(
            (0.55, 0.75),
            1.5,
            1.5,
            boxstyle="round,pad=0,rounding_size=0.22",
            facecolor=HSW.strategy,
            edgecolor="none",
        )
    )
    ax.text(
        1.30, 1.50, "HSW", ha="center", va="center", fontsize=30, fontweight="bold", color="white"
    )
    ax.text(2.55, 1.95, "HSW Stock Bot", fontsize=34, fontweight="bold", color=HSW.ink, va="center")
    ax.text(
        2.57,
        1.30,
        "A small, honest swing trading bot you can read, test and run.",
        fontsize=15,
        color=HSW.ink_soft,
        va="center",
    )
    ax.text(
        2.57,
        0.86,
        "Paper trading first  \u00b7  one engine for backtest and bot  \u00b7  "
        "safety checks that fail closed",
        fontsize=11,
        color=HSW.muted,
        va="center",
    )
    rng = np.random.default_rng(7)
    heights = 0.08 + 0.22 * rng.random(BANNER_BARS)
    step = (12 - 2 * 0.55) / BANNER_BARS
    for i, h in enumerate(heights):
        ax.add_patch(
            FancyBboxPatch(
                (0.55 + i * step, 0.14),
                step * 0.55,
                h,
                boxstyle="round,pad=0,rounding_size=0.03",
                facecolor=HSW.strategy_light if i % 5 else HSW.market,
                edgecolor="none",
                alpha=0.45,
            )
        )
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
