from __future__ import annotations

import re
from datetime import timedelta

UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
DURATION_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")


def parse_duration(text: str) -> timedelta:
    match = DURATION_PATTERN.match(text.strip())
    if match is None:
        raise ValueError(f"invalid duration {text!r}; use forms like 30s, 10m, 2h, 1d")
    amount, unit = match.groups()
    return timedelta(seconds=float(amount) * UNIT_SECONDS[unit])


def format_duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    for unit in ("d", "h", "m"):
        size = UNIT_SECONDS[unit]
        if seconds and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
