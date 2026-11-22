"""Refresh the saved regression snapshot. Run only after an intended change:

python -m tests.make_golden
"""

from __future__ import annotations

import json

from tests.test_guard_golden import GOLDEN_FILE, snapshot_values

if __name__ == "__main__":
    GOLDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_FILE.write_text(
        json.dumps(snapshot_values(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote {GOLDEN_FILE}")
