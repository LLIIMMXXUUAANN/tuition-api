"""Timetable tool implementations — port of src/features/agent/lib/tools/timetable-tools.ts."""

from __future__ import annotations

import asyncio

from supabase import AsyncClient

from app.features.agent.tools.shared import err_msg
from app.features.timetable.service import (
    BookedSlot,
    TimetableValidationError,
    run_slot_generation,
    save_buffer_mins as svc_save_buffer_mins,
    save_rules as svc_save_rules,
)
from app.shared.db import get_active_students, get_setting


async def get_timetable_settings(supabase: AsyncClient) -> dict:
    try:
        rules_raw, buffer_raw = await asyncio.gather(
            get_setting(supabase, "timetable_rules", ""),
            get_setting(supabase, "timetable_buffer_mins"),
        )
        rules: str = rules_raw or ""
        buffer_mins: int = int(buffer_raw) if buffer_raw is not None else 15
        return {"rules": rules, "buffer_mins": buffer_mins}
    except Exception as err:
        return {"error": err_msg(err)}


async def update_timetable_rules(supabase: AsyncClient, rules: str) -> dict:
    try:
        await svc_save_rules(supabase, rules)
        return {"ok": True}
    except Exception as err:
        return {"error": err_msg(err)}


async def update_buffer_mins(supabase: AsyncClient, buffer_mins: int) -> dict:
    try:
        await svc_save_buffer_mins(supabase, buffer_mins)
        return {"ok": True}
    except TimetableValidationError as err:
        return {"error": str(err)}
    except Exception as err:
        return {"error": err_msg(err)}


async def generate_slot_availability(
    supabase: AsyncClient,
    student_availability: str = "",
) -> dict:
    try:
        rules_raw, buffer_raw, students = await asyncio.gather(
            get_setting(supabase, "timetable_rules", ""),
            get_setting(supabase, "timetable_buffer_mins"),
            get_active_students(supabase, "class_schedule"),
        )
    except Exception as err:
        return {"error": err_msg(err, "Failed to fetch timetable settings")}

    rules: str = rules_raw or ""
    if not rules.strip():
        return {"error": "No timetable rules configured. Use update_timetable_rules first."}

    try:
        buffer_mins: int = int(buffer_raw) if buffer_raw is not None else 15
    except (ValueError, TypeError):
        buffer_mins = 15

    booked_slots: list[BookedSlot] = []
    for s in students:
        for slot_data in (s.get("class_schedule") or []):
            booked_slots.append(BookedSlot(
                day=slot_data["day"],
                start=slot_data["start"],
                end=slot_data["end"],
            ))

    try:
        slots = await run_slot_generation(rules, student_availability or None, booked_slots, buffer_mins)
        return {"slots": [s.model_dump() if hasattr(s, "model_dump") else {"day": s.day, "time": s.time, "state": s.state} for s in slots]}
    except Exception as err:
        return {"error": err_msg(err, "Slot generation failed")}


async def download_timetable_image(supabase: AsyncClient) -> dict:
    try:
        result = (
            await supabase.from_("students")
            .select("name, class_schedule")
            .eq("status", "Active")
            .order("name")
            .execute()
        )
    except Exception as err:
        return {"error": err_msg(err)}
    return {
        "students": [
            {"name": s["name"], "class_schedule": s.get("class_schedule") or []}
            for s in (result.data or [])
        ]
    }
