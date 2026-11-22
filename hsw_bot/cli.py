"""The ``hsw-bot`` command.

    hsw-bot backtest [--strategy NAME] [--cost-mult 2]   test a strategy on history
    hsw-bot report [--refresh]                           rebuild every number and chart in the docs
    hsw-bot dry-run                                      real data, pretend broker, no keys needed
    hsw-bot run [--live]                                 one real run (paper unless both switches)
    hsw-bot status [--live]                              account, positions, orders, safety state
    hsw-bot rebuild-state [--live] --peak-now | --peak N  rebuild lost records from the broker
    hsw-bot reset-peak [--live] [--peak N]               set a new best level (default: today)
    hsw-bot no-margin [--live]                           set the account so it cannot borrow

Exit codes: 0 = fine, 1 = finished but something needs a look, 2 = failed
closed (a safety check stopped the run; nothing was sent), 3 = a setup
problem (settings or keys).
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from hsw_bot import __version__
from hsw_bot.broker.base import Broker
from hsw_bot.config import STATE_FILE_NAME, Config, ConfigError, load_config
from hsw_bot.journal import Journal
from hsw_bot.runner import BotContext, ExitCode, Mode, RunReport, find_kill_switch, run_once

if TYPE_CHECKING:
    from hsw_bot.broker.alpaca import AlpacaBroker

MARKET_TZ = ZoneInfo("America/New_York")
log = logging.getLogger("hsw_bot")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``hsw-bot`` command. Returns the exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        setup_logging(config, verbose=args.verbose)
        code: int = args.handler(config, args)
    except ConfigError as err:
        print(f"Setup problem: {err}", file=sys.stderr)
        return int(ExitCode.SETUP_ERROR)
    except KeyboardInterrupt:
        print("Stopped by you.", file=sys.stderr)
        return int(ExitCode.FAILED_CLOSED)
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hsw-bot", description="HSW Stock Bot (education only).")
    parser.add_argument("--version", action="version", version=f"hsw-bot {__version__}")
    parser.add_argument("--config", help="path to config.toml (default: ./config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug messages")
    commands = parser.add_subparsers(dest="command", required=True)

    backtest = commands.add_parser("backtest", help="test strategies on history")
    backtest.add_argument("--strategy", action="append", help="strategy name (repeatable)")
    backtest.add_argument("--cost-mult", type=float, default=1.0, help="multiply all costs")
    backtest.add_argument("--start", help="first day, YYYY-MM-DD (default from config.toml)")
    backtest.add_argument("--end", help="last day, YYYY-MM-DD (default: newest data)")
    backtest.add_argument("--refresh", action="store_true", help="download fresh data first")
    backtest.set_defaults(handler=cmd_backtest)

    report = commands.add_parser("report", help="rebuild every number, table and chart in the docs")
    report.add_argument("--refresh", action="store_true", help="download fresh data first")
    report.set_defaults(handler=cmd_report)

    dry = commands.add_parser("dry-run", help="real data, pretend broker; shows the orders")
    dry.add_argument("--cash", type=float, help="pretend account size (default from config.toml)")
    dry.set_defaults(handler=cmd_dry_run)

    run = commands.add_parser("run", help="one real bot run on Alpaca (paper by default)")
    run.add_argument(
        "--live", action="store_true", help="live money; ALSO needs ALPACA_PAPER=false"
    )
    run.set_defaults(handler=cmd_run)

    status = commands.add_parser("status", help="show the account, positions and safety state")
    status.add_argument("--live", action="store_true", help="look at the live account")
    status.set_defaults(handler=cmd_status)

    rebuild = commands.add_parser("rebuild-state", help="rebuild lost records from the broker")
    rebuild.add_argument("--live", action="store_true", help="the live account")
    rebuild.add_argument("--replace", action="store_true", help="replace records that exist")
    rebuild.add_argument("--peak", type=float, help="the best level for the drawdown limit, in $")
    rebuild.add_argument("--peak-now", action="store_true", help="best level = today's equity")
    rebuild.add_argument(
        "--fresh-start", action="store_true", help="treat every current position as yours"
    )
    rebuild.set_defaults(handler=cmd_rebuild_state)

    reset = commands.add_parser("reset-peak", help="set a new best level for the drawdown limit")
    reset.add_argument("--live", action="store_true", help="the live account")
    reset.add_argument("--peak", type=float, help="the new best level in $ (default: today)")
    reset.set_defaults(handler=cmd_reset_peak)

    margin = commands.add_parser("no-margin", help="set the account so it cannot borrow")
    margin.add_argument("--live", action="store_true", help="the live account")
    margin.set_defaults(handler=cmd_no_margin)
    return parser


def setup_logging(config: Config, *, verbose: bool) -> None:
    """Print short messages to the screen and full ones to logs/bot.log."""
    root = logging.getLogger("hsw_bot")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    screen = logging.StreamHandler()
    screen.setLevel(logging.DEBUG if verbose else logging.INFO)
    screen.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(screen)
    config.bot.log_file.parent.mkdir(parents=True, exist_ok=True)
    disk = logging.FileHandler(config.bot.log_file, encoding="utf-8")
    disk.setLevel(logging.DEBUG)
    disk.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(disk)


# ----------------------------------------------------------------------------- backtest and report


def cmd_backtest(config: Config, args: argparse.Namespace) -> int:
    """Run backtests and print a short scorecard against both baselines."""
    from hsw_bot.report import load_bars_for, print_scorecard, study

    names = args.strategy or list(config.report.strategies)
    bars = load_bars_for(config, names, refresh=args.refresh)
    for name in names:
        result = study(
            config,
            name,
            bars,
            cost_mult=args.cost_mult,
            start=args.start,
            end=args.end,
            with_random_test=False,
        )
        print_scorecard(result)
    return int(ExitCode.OK)


def cmd_report(config: Config, args: argparse.Namespace) -> int:
    """Rebuild README.md and docs/RESULTS.md numbers, tables and charts from data."""
    from hsw_bot.report import build_report

    build_report(config, refresh=args.refresh)
    return int(ExitCode.OK)


# ----------------------------------------------------------------------------- the bot


def cmd_dry_run(config: Config, args: argparse.Namespace) -> int:
    """A full bot run on real data with a pretend broker. Needs no keys, sends nothing real."""
    from hsw_bot.broker.fake import FakeBroker

    cash = args.cash or config.bot.dry_run_cash
    broker = FakeBroker(cash=cash, now=lambda: datetime.now(MARKET_TZ))
    with tempfile.TemporaryDirectory(prefix="hsw-dry-run-") as scratch:
        report = _run_bot(config, broker, Mode.DRY_RUN, Path(scratch) / "state.json")
    print_orders(report, header=f"DRY RUN with a pretend ${cash:,.0f} account. Nothing was sent.")
    return int(report.exit_code)


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    """One real run on Alpaca. Paper unless BOTH live switches are on."""
    mode, broker = _connect(config, args.live)
    if mode is Mode.LIVE:
        log.warning("LIVE TRADING: real money. Both live switches are on.")
    report = _run_bot(config, broker, mode, config.bot.state_path(mode.value))
    print_orders(report, header=f"{mode.value.upper()} run on {broker.label}.")
    return int(report.exit_code)


def _connect(config: Config, live: bool) -> tuple[Mode, AlpacaBroker]:
    """Check the two live switches, read the keys, and open Alpaca (paper by default)."""
    from hsw_bot.broker.alpaca import AlpacaBroker
    from hsw_bot.keys import merged_env, read_keys, resolve_mode

    env = merged_env(config.root / ".env")
    mode = resolve_mode(env, live)
    keys = read_keys(env)
    broker = AlpacaBroker(
        keys.api_key,
        keys.secret_key,
        paper=mode is Mode.PAPER,
        timeout=config.bot.request_timeout_seconds,
    )
    return mode, broker


def _run_bot(config: Config, broker: Broker, mode: Mode, state_path: Path) -> RunReport:
    from hsw_bot.data import load_recent

    run_id = f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    journal = Journal(config.bot.journal_file, run_id, mode.value)
    ctx = BotContext(
        config=config,
        broker=broker,
        mode=mode,
        load_bars=lambda symbols, now: load_recent(symbols, config.bot.history_days, now),
        journal=journal,
        state_path=state_path,
        sleep=time.sleep,
    )
    return run_once(ctx)


def print_orders(report: RunReport, header: str) -> None:
    """A plain summary: where things stand, what was sent, and what needs a look."""
    print()
    print(header)
    if report.status_line:
        print(report.status_line)
    if report.market_date is not None:
        print(
            f"Decisions use the close of {report.market_date.date()}; orders are for the next open."
        )
    if report.entries_halted:
        print(f"No new buys this run: {report.entries_halted}.")
    if report.error:
        title = "FAILED CLOSED" if report.exit_code is ExitCode.FAILED_CLOSED else "STOPPED"
        print(f"{title}: {report.error}")
        if report.exit_code is ExitCode.FAILED_CLOSED:
            print("Read logs/journal.csv, fix the cause, and run again.")
    for warning in report.warnings:
        print(warning)
    if report.orders:
        _print_order_table(report)
    elif not report.error:
        print("No orders today. That is normal on most days.")
    if report.attention:
        print("NEEDS A LOOK (exit code 1):")
        for item in report.attention:
            print(f"  - {item}")


def _print_order_table(report: RunReport) -> None:
    status = {o.client_order_id: o.status for o in report.results}
    rows = [("Strategy", "Side", "Symbol", "Amount", "Close", "Broker", "Why")]
    for order in report.orders:
        plan = order.plan
        amount = f"${plan.notional:,.2f}" if plan.notional is not None else f"{plan.qty:.4f} sh"
        rows.append(
            (
                order.sleeve,
                plan.side.upper(),
                plan.symbol,
                amount,
                f"{order.price:,.2f}",
                status.get(order.client_order_id, "not sent"),
                plan.reason,
            )
        )
    _print_rows(rows)


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    """Read only: what the account holds, what is waiting, and the safety switches."""
    from hsw_bot.state import load_state

    mode, broker = _connect(config, args.live)
    path = config.bot.state_path(mode.value)
    state = load_state(path)
    account = broker.get_account()
    print(
        f"Account ({broker.label}, {account.masked_number}): equity ${account.equity:,.2f}, "
        f"cash ${account.cash:,.2f}"
    )
    if account.last_equity > 0:
        print(f"Change since the previous close: {account.equity / account.last_equity - 1:+.2%}")
    stop = find_kill_switch(config.bot.kill_switch_file.parent)
    print(f"Kill switch: {'ON (' + stop.name + ', no new buys)' if stop else 'off'}")
    if state is None:
        print(f"No bot records yet in {path}.")
    else:
        if state.peak_equity:
            print(
                f"Best level seen by the bot: ${state.peak_equity:,.2f} "
                f"({account.equity / state.peak_equity - 1:+.2%} from it)"
            )
        print(f"Last bot run (UTC): {state.last_run_utc or 'never'}")
        if state.fail_streak:
            print(f"Failed runs in a row: {state.fail_streak}")
        for symbol, carried in sorted(state.carried_sells.items()):
            tries = f"{carried.tries} failed so far, since {carried.since}"
            print(f"Sell to send again: {symbol} ({tries})")
    ignored = set(config.bot.ignore_symbols) | set(state.own_symbols if state else [])
    print("Positions:")
    for p in broker.get_positions():
        if state is not None and p.symbol in state.positions:
            owner = state.positions[p.symbol].sleeve
        else:
            owner = "yours (ignored)" if p.symbol in ignored else "NOT the bot's"
        print(f"  {p.symbol:<6} {p.qty:>12.4f} sh  ${p.market_value:>11,.2f}  {owner}")
    print("Open orders:")
    for o in broker.get_open_orders():
        print(f"  {o.side:<4} {o.symbol:<6} {o.status:<10} {o.client_order_id}")
    return int(ExitCode.OK)


def cmd_rebuild_state(config: Config, args: argparse.Namespace) -> int:
    """Rebuild lost records from the broker's order history (see hsw_bot/rebuild.py)."""
    from hsw_bot.rebuild import RebuildError, rebuild_state
    from hsw_bot.state import save_state

    mode, broker = _connect(config, args.live)
    path = config.bot.state_path(mode.value)
    if path.exists() and not args.replace:
        print(
            f"Records already exist in {path}. Add --replace to rebuild them anyway "
            "(the old file is kept next to it)."
        )
        return int(ExitCode.SETUP_ERROR)
    try:
        rebuilt = rebuild_state(
            broker,
            config.bot.client_order_prefix,
            peak=args.peak,
            peak_now=args.peak_now,
            fresh_start=args.fresh_start,
        )
    except RebuildError as err:
        print(err)
        return int(ExitCode.SETUP_ERROR)
    state = rebuilt.state
    if path.exists():
        path.replace(path.with_name(f"{path.name}.old-{datetime.now(UTC):%Y%m%d%H%M%S}"))
    save_state(state, path)
    print(f"Rebuilt the records in {path}.")
    if rebuilt.replays:
        rows = [("Symbol", "At the broker", "The bot's", "Result", "Why")]
        for r in rebuilt.replays:
            result = f"claimed ({r.record.sleeve})" if r.record else "NOT claimed"
            rows.append((r.symbol, f"{r.held:.4f}", f"{r.bot:.4f}", result, r.why))
        _print_rows(rows)
    print(f"{len(state.pending)} open order(s) tracked. Best level: ${state.peak_equity:,.2f}.")
    if rebuilt.highest is not None:
        day, value = rebuilt.highest
        print(
            f"For information: the highest daily account value since the bot's first fill was "
            f"${value:,.2f} on {day} (deposits and withdrawals included)."
        )
    unclaimed = sorted(r.symbol for r in rebuilt.replays if r.record is None)
    if args.fresh_start and state.own_symbols:
        print(f"Marked as yours (the bot leaves them alone): {', '.join(state.own_symbols)}")
    elif unclaimed:
        print(
            f"Not claimed for the bot: {', '.join(unclaimed)}. Add them to bot.ignore_symbols in "
            "config.toml, or close them by hand. Until then each run asks for a look."
        )
    old = config.bot.state_dir / STATE_FILE_NAME
    if old.is_file():
        print(f"An old records file is still in {old}. Move it out of that folder, or runs refuse.")
    return int(ExitCode.OK)


def cmd_reset_peak(config: Config, args: argparse.Namespace) -> int:
    """Set a new best account level for the drawdown limit, and write it in the journal."""
    from hsw_bot.state import checked_peak, load_state, save_state

    mode, broker = _connect(config, args.live)
    path = config.bot.state_path(mode.value)
    state = load_state(path)
    if state is None:
        print(f"No bot records yet in {path}. The first run sets the best level by itself.")
        return int(ExitCode.SETUP_ERROR)
    account = broker.get_account()
    if state.account_id != account.account_id:
        print("These records belong to another account. Check the keys in .env.")
        return int(ExitCode.SETUP_ERROR)
    try:
        new = checked_peak(account.equity if args.peak is None else args.peak)
    except ValueError as err:
        print(f"That best level cannot be used ({err}).")
        return int(ExitCode.SETUP_ERROR)
    old = state.peak_equity
    state.peak_equity = new
    save_state(state, path)
    journal = Journal(
        config.bot.journal_file, f"reset-{datetime.now(UTC):%Y%m%d-%H%M%S}", mode.value
    )
    was = f"${old:,.2f}" if old else "not set"
    journal.write("peak_reset", f"best level changed by hand: {was} -> ${new:,.2f}")
    print(f"{mode.value} account: best level {was} -> ${new:,.2f}.")
    if find_kill_switch(config.bot.kill_switch_file.parent) is not None:
        print("A STOP file still exists, so there are still no new buys until you delete it.")
    return int(ExitCode.OK)


def cmd_no_margin(config: Config, args: argparse.Namespace) -> int:
    """Set ONLY the account's max margin multiplier to 1, then show every setting."""
    mode, broker = _connect(config, args.live)
    before = broker.max_margin_multiplier()
    settings = broker.forbid_margin()
    after = settings.get("max_margin_multiplier", "unknown")
    print(f"{mode.value} account: max margin multiplier was {before}, now {after}.")
    print("All account settings, as the broker reports them now:")
    for name, value in sorted(settings.items()):
        print(f"  {name}: {value}")
    if after != "1":
        print("The broker did not accept the change. Check the account in the Alpaca dashboard.")
        return int(ExitCode.NEEDS_ATTENTION)
    print("The account can no longer borrow.")
    return int(ExitCode.OK)


def _print_rows(rows: Sequence[tuple[str, ...]]) -> None:
    """A plain table: every column but the last padded to its widest cell."""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    for row in rows:
        cells = [cell.ljust(widths[i]) for i, cell in enumerate(row[:-1])]
        print("  ".join([*cells, row[-1]]))


if __name__ == "__main__":
    raise SystemExit(main())
