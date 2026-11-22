"""Test the tests: plant a known bug, and check that its guard test catches it.

A guard test that never fails is worth nothing. This script copies the code to
a temporary folder, breaks it on purpose in one place, runs the matching guard
test, and repeats for each bug below. Every line should say CAUGHT.

    python -m tests.plant_bugs

It changes nothing in your own copy of the code.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class PlantedBug:
    what: str
    file: str
    find: str
    replace: str
    guard: str


BUGS = (
    PlantedBug(
        "an indicator reads tomorrow's close",
        "hsw_bot/strategies/swing.py",
        "        close = bars.close\n        fast",
        "        close = bars.close.shift(-1)\n        fast",
        "tests/test_guard_lookahead.py",
    ),
    PlantedBug(
        "buys fill at the open of the decision day",
        "hsw_bot/engine.py",
        "        open_price = opens[i, column[order.symbol]]\n"
        "        if order.symbol in account.positions",
        "        open_price = opens[i - 1, column[order.symbol]]\n"
        "        if order.symbol in account.positions",
        "tests/test_guard_lookahead.py",
    ),
    PlantedBug(
        "sells fill above the open (cost charged the wrong way)",
        "hsw_bot/engine.py",
        "price = open_price * (1 - slip)",
        "price = open_price * (1 + slip)",
        "tests/test_guard_costs.py",
    ),
    PlantedBug(
        "the sell commission is never taken from the cash",
        "hsw_bot/engine.py",
        "account.cash += position.qty * price - fee",
        "account.cash += position.qty * price",
        "tests/test_guard_reconciliation.py",
    ),
    PlantedBug(
        "a missing price is not carried forward",
        "hsw_bot/engine.py",
        'marks = bars.close.ffill().to_numpy(dtype="float64")',
        'marks = bars.close.to_numpy(dtype="float64")',
        "tests/test_guard_finite.py",
    ),
    PlantedBug(
        "buys pay a tiny bit more than they should",
        "hsw_bot/engine.py",
        "price = open_price * (1 + slip)",
        "price = open_price * (1 + slip) * 1.0001",
        "tests/test_guard_golden.py",
    ),
)


def plant(bug: PlantedBug) -> bool:
    """Return True if the guard test failed (the bug was caught)."""
    with tempfile.TemporaryDirectory(prefix="hsw-plant-") as scratch:
        work = Path(scratch)
        for name in ("hsw_bot", "tests"):
            shutil.copytree(REPO / name, work / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("config.toml", "pyproject.toml"):
            shutil.copy(REPO / name, work / name)
        target = work / bug.file
        text = target.read_text(encoding="utf-8")
        if text.count(bug.find) != 1:
            raise SystemExit(f"Could not plant '{bug.what}': the code changed. Update this script.")
        target.write_text(text.replace(bug.find, bug.replace), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", bug.guard, "-q", "-x", "-p", "no:cacheprovider"],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode != 0


def main() -> int:
    missed = 0
    for bug in BUGS:
        caught = plant(bug)
        missed += not caught
        print(f"{'CAUGHT' if caught else 'MISSED'}  {bug.what}  ({bug.guard})")
    print("All planted bugs were caught." if not missed else f"{missed} bug(s) slipped through!")
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
