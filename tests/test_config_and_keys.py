"""Settings are checked with clear messages, and live trading needs two switches."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hsw_bot.config import ConfigError, load_config
from hsw_bot.keys import AlpacaKeys, merged_env, read_env_file, read_keys, resolve_mode
from hsw_bot.runner import Mode, check_sleeves
from hsw_bot.strategies import STRATEGIES, build_strategy

REPO = Path(__file__).resolve().parent.parent


def copy_config(tmp: Path, old: str = "", new: str = "") -> Path:
    path = tmp / "config.toml"
    shutil.copy(REPO / "config.toml", path)
    if old:
        text = path.read_text(encoding="utf-8")
        assert old in text
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return path


def test_the_shipped_config_loads_and_builds_every_strategy() -> None:
    config = load_config(REPO / "config.toml")
    assert config.costs.slippage_bps > 0
    assert config.costs.commission_per_order == 0
    for name in STRATEGIES:
        strategy = build_strategy(config, name)
        assert strategy.universe
    check_sleeves({n: build_strategy(config, n) for n in config.bot.sleeves})


def test_sp100_list_is_documented_size() -> None:
    config = load_config(REPO / "config.toml")
    assert len(config.universes["sp100"]) == 100
    assert "GOOG" not in config.universes["sp100"]


def test_a_typo_in_a_setting_is_refused(tmp_path: Path) -> None:
    path = copy_config(tmp_path, "stop_atr = 3.0", "stop_atrr = 3.0")
    config = load_config(path)
    with pytest.raises(ConfigError, match="unknown setting"):
        build_strategy(config, "swing")


def test_a_wrong_type_is_refused(tmp_path: Path) -> None:
    path = copy_config(tmp_path, "slippage_bps = 5.0", 'slippage_bps = "five"')
    with pytest.raises(ConfigError, match="should be a number"):
        load_config(path)


def test_sleeves_over_100_percent_are_refused(tmp_path: Path) -> None:
    path = copy_config(
        tmp_path,
        "sleeves = { steady = 0.5, swing = 0.5 }",
        "sleeves = { steady = 0.7, swing = 0.5 }",
    )
    with pytest.raises(ConfigError, match="add up"):
        load_config(path)


@pytest.mark.parametrize("name", ["my-idea", "my idea", "idée"])
def test_a_strategy_name_must_be_plain(tmp_path: Path, name: str) -> None:
    """The name goes into every order id; a dash (or anything unusual) would break it."""
    path = copy_config(tmp_path, "[strategies.swing]", f'[strategies."{name}"]')
    with pytest.raises(ConfigError, match="letters a to z, digits and _"):
        load_config(path)


def test_two_strategies_may_not_share_a_stock() -> None:
    config = load_config(REPO / "config.toml")
    both = {
        "swing": build_strategy(config, "swing"),
        "my_strategy": build_strategy(config, "my_strategy"),
    }
    with pytest.raises(ConfigError, match="both"):
        check_sleeves(both)


@pytest.mark.parametrize(
    ("paper", "live_flag", "expected"),
    [
        (None, False, Mode.PAPER),
        ("true", False, Mode.PAPER),
        ("false", True, Mode.LIVE),
    ],
)
def test_paper_by_default_and_live_only_with_both_switches(
    paper: str | None, live_flag: bool, expected: Mode
) -> None:
    env = {} if paper is None else {"ALPACA_PAPER": paper}
    assert resolve_mode(env, live_flag) is expected


@pytest.mark.parametrize(("paper", "live_flag"), [("true", True), (None, True), ("false", False)])
def test_one_switch_alone_is_refused(paper: str | None, live_flag: bool) -> None:
    env = {} if paper is None else {"ALPACA_PAPER": paper}
    with pytest.raises(ConfigError, match="both"):
        resolve_mode(env, live_flag)


def test_a_garbled_paper_switch_is_refused() -> None:
    with pytest.raises(ConfigError, match="true or false"):
        resolve_mode({"ALPACA_PAPER": "maybe"}, False)


def test_keys_are_read_but_never_shown(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# my keys\nALPACA_API_KEY=PKTESTKEY123\nALPACA_SECRET_KEY='very-secret-456'\n",
        encoding="utf-8",
    )
    env = merged_env(env_file, environ={})
    keys = read_keys(env)
    assert keys.api_key == "PKTESTKEY123"
    assert keys.secret_key == "very-secret-456"
    assert "PKTESTKEY123" not in repr(keys) and "very-secret-456" not in repr(keys)
    assert "very-secret-456" not in str(keys)


def test_environment_beats_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ALPACA_PAPER=true\n", encoding="utf-8")
    assert read_env_file(env_file)["ALPACA_PAPER"] == "true"
    assert merged_env(env_file, environ={"ALPACA_PAPER": "false"})["ALPACA_PAPER"] == "false"


def test_missing_keys_explain_what_to_do() -> None:
    with pytest.raises(ConfigError) as caught:
        read_keys({"ALPACA_API_KEY": "PKONLYTHIS"})
    message = str(caught.value)
    assert "ALPACA_SECRET_KEY" in message and ".env" in message
    assert "PKONLYTHIS" not in message
    assert isinstance(AlpacaKeys("a", "b"), AlpacaKeys)
