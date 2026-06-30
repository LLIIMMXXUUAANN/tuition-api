"""Shared utility helpers ported from src/lib/utils.ts."""

import calendar
import datetime
from collections import defaultdict

from app.types import ClassSlot, WeekDay

DAYS: list[str] = [d.value for d in WeekDay]

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# JS-compatible day index: Sunday=0, Monday=1, … Saturday=6
DAY_INDEX: dict[str, int] = {
    "Sunday": 0,
    "Monday": 1,
    "Tuesday": 2,
    "Wednesday": 3,
    "Thursday": 4,
    "Friday": 5,
    "Saturday": 6,
}

# 30-min slots 08:00 … 21:30
TIME_SLOTS: list[str] = []
h = 8
m = 0
while (h, m) <= (21, 30):
    TIME_SLOTS.append(f"{h:02d}:{m:02d}")
    m += 30
    if m == 60:
        m = 0
        h += 1


def time_to_mins(time: str) -> int:
    """'14:30' → 870"""
    parts = time.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def format_fee(fee: float) -> str:
    """Round to 2 dp, strip trailing .00."""
    rounded = round(fee, 2)
    formatted = f"{rounded:.2f}"
    if formatted.endswith(".00"):
        return formatted[:-3]
    return formatted


def ordinal(n: int) -> str:
    """1 → '1st', 2 → '2nd', 11 → '11th'"""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def oxford_list(items: list[str]) -> str:
    """['a', 'b', 'c'] → 'a, b, and c'"""
    if len(items) == 0:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def group_slots_by_day(schedule: list[ClassSlot]) -> dict[str, list[ClassSlot]]:
    """Group ClassSlot list by day, preserving insertion order."""
    result: dict[str, list[ClassSlot]] = defaultdict(list)
    for slot in schedule:
        result[slot.day].append(slot)
    return dict(result)


def get_weekday_dates(year: int, month: int, weekday: str) -> list[int]:
    """Return day numbers in (year, month) that fall on `weekday`.

    Uses JS-compatible DAY_INDEX mapping (Sunday=0, Monday=1, …, Saturday=6).
    Python's weekday(): Monday=0 … Sunday=6, so we convert:
      python_wd = (js_day_index - 1) % 7
    """
    js_idx = DAY_INDEX[weekday]
    # Python calendar: Monday=0 … Sunday=6
    python_wd = (js_idx - 1) % 7

    dates = []
    _, days_in_month = calendar.monthrange(year, month)
    for day in range(1, days_in_month + 1):
        if datetime.date(year, month, day).weekday() == python_wd:
            dates.append(day)
    return dates
