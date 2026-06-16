"""Student tool implementations."""

from __future__ import annotations

import datetime

import pytz
from supabase import AsyncClient

from app.features.agent.tools.shared import err_msg
from app.features.google.sync import sync_all_students
from app.features.google.auth import get_oauth2_credentials, save_token_if_rotated
from app.features.students.service import (
    StudentNotFoundError,
    create_student as svc_create,
    update_student as svc_update,
    delete_student as svc_delete,
)
from app.shared.utils import get_weekday_dates, group_slots_by_day, time_to_mins
from app.types import ClassSlot

ALLOWED_UPDATE_KEYS: set[str] = {
    "name", "mode", "fee_per_hour", "payment_method", "status",
    "class_schedule", "contact_person", "contact_phone", "student_phone",
    "today_homework", "notes", "latest_payment",
    "google_meet_link", "google_drive_link", "access_emails",
}


async def search_students(supabase: AsyncClient, query: str) -> dict:
    result = (
        await supabase.from_("students")
        .select("id, name, status, class_schedule")
        .ilike("name", f"%{query}%")
        .order("name")
        .execute()
    )
    if hasattr(result, "error") and result.error:
        return {"error": result.error.message}
    return {"students": result.data or []}


async def get_student(supabase: AsyncClient, id: str) -> dict:
    result = (
        await supabase.from_("students")
        .select(
            "id, name, status, mode, fee_per_hour, payment_method, class_schedule, "
            "contact_person, contact_phone, student_phone, today_homework, notes, "
            "latest_payment, google_meet_link, google_drive_link, calendar_event_ids, access_emails"
        )
        .eq("id", id)
        .maybe_single()
        .execute()
    )
    if result is None or not result.data:
        return {"error": "Student not found"}
    if hasattr(result, "error") and result.error:
        return {"error": result.error.message}
    return {"student": result.data}


async def manage_portal_access(
    supabase: AsyncClient,
    student_id: str,
    action: str,  # 'add' | 'remove'
    email: str,
) -> dict:
    normalised = email.strip().lower()

    fetch_result = (
        await supabase.from_("students")
        .select("access_emails")
        .eq("id", student_id)
        .maybe_single()
        .execute()
    )
    if fetch_result is None or (hasattr(fetch_result, "error") and fetch_result.error) or not fetch_result.data:
        return {"error": "Student not found"}

    current: list[str] = fetch_result.data.get("access_emails") or []

    if action == "add":
        if normalised in current:
            return {"result": f"{normalised} already has access"}
        updated = [*current, normalised]
    else:
        if normalised not in current:
            return {"result": f"{normalised} does not have access"}
        updated = [e for e in current if e != normalised]

    update_result = (
        await supabase.from_("students")
        .update({"access_emails": updated})
        .eq("id", student_id)
        .execute()
    )
    if hasattr(update_result, "error") and update_result.error:
        return {"error": update_result.error.message}

    if action == "add":
        return {"result": f"{normalised} can now log in to the student portal"}
    return {"result": f"{normalised} has been removed from portal access"}


async def list_students(supabase: AsyncClient, params: dict) -> dict:
    valid_statuses = {"Active", "On Hold", "Completed"}
    status = params.get("status")
    if status and status not in valid_statuses:
        return {"error": f"Invalid status: {status}"}

    query = (
        supabase.from_("students")
        .select("id, name, status, mode, fee_per_hour, class_schedule")
        .order("name")
    )
    if status:
        query = query.eq("status", status)

    result = await query.execute()
    if hasattr(result, "error") and result.error:
        return {"error": result.error.message}
    return {"students": result.data or []}


async def create_student(supabase: AsyncClient, params: dict) -> dict:
    try:
        result = await svc_create(supabase, params)
    except Exception as exc:
        return {"error": err_msg(exc)}
    response: dict = {"id": result["id"], "name": result["name"]}
    if result.get("google_warning"):
        response["google_warning"] = result["google_warning"]
    return response


async def update_student(supabase: AsyncClient, id: str, fields: dict) -> dict:
    permitted = {k: v for k, v in fields.items() if k in ALLOWED_UPDATE_KEYS}
    if not permitted:
        return {"error": "No valid fields to update"}

    try:
        result = await svc_update(supabase, id, permitted)
    except StudentNotFoundError:
        return {"error": "Student not found"}
    except Exception as exc:
        return {"error": err_msg(exc)}

    response: dict = {"success": True}
    if result.get("google_warning"):
        response["googleWarnings"] = [result["google_warning"]]
    return response


async def delete_student(supabase: AsyncClient, id: str) -> dict:
    try:
        result = await svc_delete(supabase, id)
    except StudentNotFoundError:
        return {"error": "Student not found"}
    except Exception as exc:
        return {"error": err_msg(exc)}

    response: dict = {"success": True}
    if result.get("google_warning"):
        response["warnings"] = [result["google_warning"]]
    return response


async def run_sync_all(supabase: AsyncClient) -> dict:
    try:
        creds, stored_token = await get_oauth2_credentials(supabase)
        results = await sync_all_students(supabase, creds)
        await save_token_if_rotated(creds, stored_token, supabase)
        return {"results": results}
    except Exception as err:
        msg = err_msg(err, "Google auth failed")
        if "invalid_grant" in msg:
            return {"error": "Google auth expired — reconnect at /api/google/auth"}
        return {"error": msg}


async def get_schedule(supabase: AsyncClient, day: str) -> dict:
    result = (
        await supabase.from_("students")
        .select("id, name, class_schedule")
        .eq("status", "Active")
        .execute()
    )
    if hasattr(result, "error") and result.error:
        return {"error": result.error.message}

    students = []
    for s in (result.data or []):
        schedule = s.get("class_schedule") or []
        slots = [
            {"start": slot["start"], "end": slot["end"]}
            for slot in schedule
            if slot.get("day") == day
        ]
        if slots:
            students.append({"id": s["id"], "name": s["name"], "slots": slots})

    return {"day": day, "students": students}


async def get_fee_summary(
    supabase: AsyncClient,
    month: int | None = None,
    year: int | None = None,
) -> dict:
    MYT = pytz.timezone("Asia/Kuala_Lumpur")
    now_myt = datetime.datetime.now(tz=MYT)
    resolved_month = month if month is not None else now_myt.month
    resolved_year = year if year is not None else now_myt.year

    result = (
        await supabase.from_("students")
        .select("id, name, fee_per_hour, class_schedule")
        .eq("status", "Active")
        .execute()
    )
    if hasattr(result, "error") and result.error:
        return {"error": result.error.message}

    raw_fees: list[float] = []
    students_out: list[dict] = []

    for s in (result.data or []):
        schedule = s.get("class_schedule") or []
        slots = [ClassSlot(**slot) for slot in schedule]
        slots_by_day = group_slots_by_day(slots)
        fee = 0.0
        for day, day_slots in slots_by_day.items():
            dates = get_weekday_dates(resolved_year, resolved_month, day)
            hours_per_session = sum(
                (time_to_mins(slot.end) - time_to_mins(slot.start)) / 60
                for slot in day_slots
            )
            fee += len(dates) * hours_per_session * s["fee_per_hour"]
        raw_fees.append(fee)
        students_out.append({
            "id": s["id"],
            "name": s["name"],
            "fee": round(fee * 100) / 100,
        })

    total = round(sum(raw_fees) * 100) / 100
    return {
        "month": resolved_month,
        "year": resolved_year,
        "students": students_out,
        "total": total,
    }
