"""The journal: one CSV row for every decision, order, fill, mismatch and safety stop.

Open ``logs/journal.csv`` in any spreadsheet to see exactly what the bot did
and why. When something looks wrong, this file is the first place to look.
"""

from __future__ import annotations

import csv
import logging
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("hsw_bot")

COLUMNS = (
    "time_utc",
    "run_id",
    "mode",
    "sleeve",
    "event",
    "symbol",
    "side",
    "qty",
    "notional",
    "price",
    "status",
    "detail",
)

WARNING_EVENTS = frozenset({"mismatch", "unmanaged", "skip_order", "order_problem"})
ERROR_EVENTS = frozenset({"fail_closed", "order_error"})


class Journal:
    """Appends rows to the journal CSV and mirrors each row to the log."""

    def __init__(self, path: Path, run_id: str, mode: str) -> None:
        self.path = path
        self.run_id = run_id
        self.mode = mode
        self.rows: list[dict[str, str]] = []

    def write(
        self,
        event: str,
        detail: str = "",
        *,
        sleeve: str = "",
        symbol: str = "",
        side: str = "",
        qty: float | None = None,
        notional: float | None = None,
        price: float | None = None,
        status: str = "",
    ) -> None:
        row = {
            "time_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "mode": self.mode,
            "sleeve": sleeve,
            "event": event,
            "symbol": symbol,
            "side": side,
            "qty": "" if qty is None else f"{qty:.9g}",
            "notional": "" if notional is None else f"{notional:.2f}",
            "price": "" if price is None else f"{price:.4f}",
            "status": status,
            "detail": detail,
        }
        self.rows.append(row)
        self._append(row)
        text = " ".join(p for p in (event, sleeve, side, symbol, status, detail) if p)
        if event in ERROR_EVENTS:
            log.error(text)
        elif event in WARNING_EVENTS:
            log.warning(text)
        else:
            log.info(text)

    def _append(self, row: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
