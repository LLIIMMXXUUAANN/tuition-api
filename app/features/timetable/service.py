"""Timetable slot helpers — port of src/features/timetable/lib/timetable-slots.ts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from supabase import AsyncClient

from app.shared.utils import DAYS, TIME_SLOTS, time_to_mins
from app.types import SlotState

if TYPE_CHECKING:
    from app.shared.gemini.slot_generation import ClassifiedSlot


class TimetableValidationError(Exception):
    pass


async def save_rules(supabase: AsyncClient, rules: str) -> None:
    """Persist timetable_rules to the settings table."""
    await supabase.from_("settings").upsert(
        {"key": "timetable_rules", "value": rules}, on_conflict="key"
    ).execute()


async def save_buffer_mins(supabase: AsyncClient, buffer_mins: int) -> None:
    """Persist timetable_buffer_mins to the settings table. Raises TimetableValidationError if out of range."""
    if buffer_mins < 0 or buffer_mins > 60:
        raise TimetableValidationError("buffer_mins must be 0–60")
    await supabase.from_("settings").upsert(
        {"key": "timetable_buffer_mins", "value": str(buffer_mins)}, on_conflict="key"
    ).execute()


@dataclass(frozen=True)
class BookedSlot:
    day: str
    start: str
    end: str


def compute_buffer_slots(booked_slots: list[BookedSlot], buffer_mins: int) -> set[str]:
    """Return set of 'Day|HH:MM' cell keys within bufferMins of any booked class."""
    buffer_set: set[str] = set()
    if buffer_mins <= 0:
        return buffer_set

    for ts in TIME_SLOTS:
        cell_start = time_to_mins(ts)
        cell_end = cell_start + 30

        for slot in booked_slots:
            booked_start = time_to_mins(slot.start)
            booked_end = time_to_mins(slot.end)

            # Gap before this cell (booked class ends before the cell starts)
            gap_before = cell_start - booked_end
            # Gap after this cell (cell ends before the booked class starts)
            gap_after = booked_start - cell_end

            if 0 <= gap_before < buffer_mins or 0 <= gap_after < buffer_mins:
                buffer_set.add(f"{slot.day}|{ts}")

    return buffer_set


def build_booked_cell_set(booked_slots: list[BookedSlot]) -> set[str]:
    """Return 'Day|HH:MM' keys for all 30-min cells overlapping any booked class."""
    booked_set: set[str] = set()
    for ts in TIME_SLOTS:
        cell_start = time_to_mins(ts)
        cell_end = cell_start + 30

        for slot in booked_slots:
            slot_start = time_to_mins(slot.start)
            slot_end = time_to_mins(slot.end)

            # Interval overlap: cell_start < slot_end AND cell_end > slot_start
            if cell_start < slot_end and cell_end > slot_start:
                booked_set.add(f"{slot.day}|{ts}")

    return booked_set


def build_slot_prompt(
    rules: str,
    student_availability: str,
    buffer_slots: set[str],
    booked_cell_set: set[str],
) -> str:
    """Build the Gemini classification prompt."""
    classifiable_lines: list[str] = []
    for day in DAYS:
        for ts in TIME_SLOTS:
            key = f"{day}|{ts}"
            if key not in booked_cell_set and key not in buffer_slots:
                classifiable_lines.append(f"- {day} {ts}")

    classifiable_slots = "\n".join(classifiable_lines) or "None"

    availability_section = student_availability.strip() if student_availability else "Not provided"

    return f"""You are a scheduling assistant for a private tutor. Classify every listed slot as "preferred", "normal", or "unavailable".

TUTOR'S SCHEDULING RULES:
{rules}

STUDENT'S AVAILABILITY:
{availability_section}

SLOTS TO CLASSIFY (return exactly these, no others — booked and buffer zones are already excluded):
{classifiable_slots}

INSTRUCTIONS:
- Classify every slot in the list above as "preferred", "normal", or "unavailable".
- "preferred" — tutor prefers this day AND the student EXPLICITLY mentioned they are available at that time
- "normal" — tutor day is normal (Wed/Sat/Sun), OR student did not mention this time, OR no student availability was provided
- "unavailable" — blocked by tutor rules (restricted hours, day limits) OR student EXPLICITLY said they cannot attend
- Time-range boundaries are EXCLUSIVE at the end: "08:00 to 10:00 unavailable" blocks the 08:00, 08:30, 09:00, and 09:30 slots but NOT 10:00 — the 10:00 slot starts after the block ends and is fully available. Never apply any extra margin around unavailability boundaries.

CRITICAL — how to interpret student availability:
- Student availability describes only times they CAN attend. They do NOT list times they cannot.
- Example: "free Thursday 12pm–6pm" confirms Thu 12:00–18:00 as available. Thu before 12pm or after 6pm is UNCLEAR, not unavailable → mark normal (subject to tutor blocked times).
- Never infer unavailability from silence. Only mark "unavailable" if tutor rules block it."""


async def run_slot_generation(
    rules: str,
    student_availability: str | None,
    booked_slots: list[BookedSlot],
    buffer_mins: int,
) -> list["ClassifiedSlot"]:
    """Orchestrate buffer computation → prompt build → Gemini call → result list."""
    from app.shared.gemini.slot_generation import ClassifiedSlot, run_gemini_slot_generation

    buffer_set = compute_buffer_slots(booked_slots, buffer_mins)
    booked_cell_set = build_booked_cell_set(booked_slots)
    prompt = build_slot_prompt(
        rules,
        student_availability or "",
        buffer_set,
        booked_cell_set,
    )

    raw_slots = await run_gemini_slot_generation(prompt)

    # Post-processing safety net: force any buffer slot that slipped through to unavailable
    result: list[ClassifiedSlot] = []
    for slot in raw_slots:
        key = f"{slot.day}|{slot.time}"
        if key in buffer_set:
            result.append(ClassifiedSlot(day=slot.day, time=slot.time, state=SlotState.unavailable))
        else:
            result.append(slot)

    return result
