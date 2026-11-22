"""The report writes only between its markers, and the command line behaves."""

from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd
import pytest

from hsw_bot import stats
from hsw_bot.cli import build_parser, main
from hsw_bot.report import md_table, pct, replace_block, sharpe_sentence
from hsw_bot.runner import ExitCode

REPO = Path(__file__).resolve().parent.parent


def test_replace_block_only_touches_its_own_markers() -> None:
    text = "intro\n<!-- report:a:start -->\nold\n<!-- report:a:end -->\nmiddle\n"
    out = replace_block(text, "a", "new table")
    assert out == "intro\n<!-- report:a:start -->\nnew table\n<!-- report:a:end -->\nmiddle\n"
    assert replace_block(out, "a", "new table") == out, "running twice changes nothing"
    with pytest.raises(ValueError, match="not found"):
        replace_block(text, "missing", "x")


def test_every_marker_in_the_docs_is_well_formed() -> None:
    for page in (REPO / "README.md", REPO / "docs" / "RESULTS.md"):
        text = page.read_text(encoding="utf-8")
        starts = text.count(":start -->")
        assert starts > 0
        assert starts == text.count(":end -->"), page


def test_formatting_helpers() -> None:
    assert pct(0.0768) == "7.7%"
    assert pct(-0.3127, 0) == "-31%"
    assert pct(0.05, sign=True) == "+5.0%"
    assert pct(None) == "n/a"
    table = md_table(["A", "B"], [["x", "1"]])
    assert table.splitlines()[1] == "| :--- | ---: |"


def test_a_small_sharpe_gap_is_called_about_the_same() -> None:
    """0.64 against 0.62 is noise, so the verdict must not call it better."""
    close = sharpe_sentence(0.64, 0.62)
    assert "about the same" in close
    assert "more return" not in close
    assert "about the same" in sharpe_sentence(0.56, 0.62)
    assert "more return" in sharpe_sentence(0.90, 0.62)
    assert "less return" in sharpe_sentence(0.30, 0.62)


def test_metrics_on_a_known_curve() -> None:
    days = pd.bdate_range("2020-01-01", periods=505)
    curve = pd.Series(100.0, index=days)
    curve.iloc[100:] = 50.0  # halves...
    curve.iloc[300:] = 121.0  # ...then ends 21% up
    m = stats.curve_metrics(curve, 100.0)
    assert m.max_drawdown == pytest.approx(-0.5)
    assert m.total_return == pytest.approx(0.21)
    years = (days[-1] - days[0]).days / 365.25
    assert m.cagr == pytest.approx(1.21 ** (1 / years) - 1)
    worst = stats.worst_drawdowns(curve, 100.0)[0]
    assert worst.depth == pytest.approx(-0.5)
    assert worst.peak == days[99] or worst.peak == days[0]


def copy_repo_config(tmp: Path) -> Path:
    path = tmp / "config.toml"
    shutil.copy(REPO / "config.toml", path)
    return path


def test_version_and_help() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(["--version"])
    assert caught.value.code == 0


def test_live_flag_alone_is_refused_before_anything_happens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("ALPACA_API_KEY", "PKSHOULDNOTPRINT")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SECRETSHOULDNOTPRINT")
    code = main(["--config", str(copy_repo_config(tmp_path)), "run", "--live"])
    out = capsys.readouterr()
    assert code == int(ExitCode.SETUP_ERROR)
    assert "both" in out.err
    assert "SHOULDNOTPRINT" not in out.out + out.err


def test_missing_keys_give_a_setup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER"):
        monkeypatch.delenv(name, raising=False)
    code = main(["--config", str(copy_repo_config(tmp_path)), "run"])
    assert code == int(ExitCode.SETUP_ERROR)
    assert "dry-run" in capsys.readouterr().err


def test_rebuild_state_and_reset_peak_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two record commands, end to end, against the fake broker."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from hsw_bot import cli
    from hsw_bot.broker.base import BrokerPosition
    from hsw_bot.broker.fake import FakeBroker
    from hsw_bot.runner import Mode
    from hsw_bot.state import load_state

    evening = datetime(2026, 11, 20, 18, 0, tzinfo=ZoneInfo("America/New_York"))
    broker = FakeBroker(cash=9_970.0, now=lambda: evening)
    broker.positions["ZZZ"] = BrokerPosition("ZZZ", 3.0, 10.0, 30.0)  # bought by hand
    monkeypatch.setattr(cli, "_connect", lambda config, live: (Mode.PAPER, broker))
    config = str(copy_repo_config(tmp_path))
    assert main(["--config", config, "rebuild-state"]) == int(ExitCode.SETUP_ERROR)
    assert "--peak-now" in capsys.readouterr().out, "it asks you to choose the best level"
    assert main(["--config", config, "rebuild-state", "--peak-now"]) == int(ExitCode.OK)
    out = capsys.readouterr().out
    assert "ZZZ" in out and "NOT claimed" in out and "the bot never bought it" in out
    records = tmp_path / "state" / "paper" / "bot_state.json"
    state = load_state(records)
    assert state is not None and state.peak_equity == pytest.approx(10_000.0)
    assert main(["--config", config, "reset-peak", "--peak", "12000"]) == int(ExitCode.OK)
    state = load_state(records)
    assert state is not None and state.peak_equity == 12_000.0
    journal = pd.read_csv(tmp_path / "logs" / "journal.csv")
    assert "peak_reset" in set(journal["event"])
