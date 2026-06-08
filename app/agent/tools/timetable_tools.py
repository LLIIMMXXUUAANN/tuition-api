"""Timetable tool implementations — port of src/features/agent/lib/tools/timetable-tools.ts."""

from __future__ import annotations

import asyncio

from supabase import AsyncClient

from app.agent.tools.shared import err_msg
from app.lib.timetable_slots import BookedSlot, run_slot_generation
from app.types import ClassSlot


async def get_timetable_settings(supabase: AsyncClient) -> dict:
    try:
        rules_result, buffer_result = await asyncio.gather(
            supabase.from_("settings").select("value").eq("key", "timetable_rules").maybe_single().execute(),
            supabase.from_("settings").select("value").eq("key", "timetable_buffer_mins").maybe_single().execute(),
        )
        rules: str = (rules_result.data or {}).get("value", "")
        raw_buf = (buffer_result.data or {}).get("value")
        buffer_mins: int = int(raw_buf) if raw_buf is not None else 15
        return {"rules": rules, "buffer_mins": buffer_mins}
    except Exception as err:
        return {"error": err_msg(err)}


async def update_timetable_rules(supabase: AsyncClient, rules: str) -> dict:
    try:
        await supabase.from_("settings").upsert(
            {"key": "timetable_rules", "value": rules}, on_conflict="key"
        ).execute()
        return {"ok": True}
    except Exception as err:
        return {"error": err_msg(err)}


async def update_buffer_mins(supabase: AsyncClient, buffer_mins: int) -> dict:
    if buffer_mins < 0 or buffer_mins > 60:
        return {"error": "buffer_mins must be 0–60"}
    try:
        await supabase.from_("settings").upsert(
            {"key": "timetable_buffer_mins", "value": str(buffer_mins)}, on_conflict="key"
        ).execute()
        return {"ok": True}
    except Exception as err:
        return {"error": err_msg(err)}


async def generate_slot_availability(
    supabase: AsyncClient,
    student_availability: str = "",
) -> dict:
    try:
        rules_result, buffer_result, students_result = await asyncio.gather(
            supabase.from_("settings").select("value").eq("key", "timetable_rules").maybe_single().execute(),
            supabase.from_("settings").select("value").eq("key", "timetable_buffer_mins").maybe_single().execute(),
            supabase.from_("students").select("class_schedule").eq("status", "Active").execute(),
        )
    except Exception as err:
        return {"error": err_msg(err, "Failed to fetch timetable settings")}

    rules: str = (rules_result.data or {}).get("value", "")
    if not rules.strip():
        return {"error": "No timetable rules configured. Use update_timetable_rules first."}

    raw_buf = (buffer_result.data or {}).get("value")
    buffer_mins: int = int(raw_buf) if raw_buf is not None else 15

    booked_slots: list[BookedSlot] = []
    for s in (students_result.data or []):
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
    result = (
        await supabase.from_("students")
        .select("name, class_schedule")
        .eq("status", "Active")
        .order("name")
        .execute()
    )
    if hasattr(result, "error") and result.error:
        return {"error": result.error.message}
    return {
        "students": [
            {"name": s["name"], "class_schedule": s.get("class_schedule") or []}
            for s in (result.data or [])
        ]
    }
