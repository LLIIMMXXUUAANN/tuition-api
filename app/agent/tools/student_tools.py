"""Student tool implementations — port of src/features/agent/lib/tools/student-tools.ts."""

from __future__ import annotations

import asyncio
import datetime

import pytz
from supabase import AsyncClient

from app.agent.tools.shared import err_msg
from app.lib.utils import get_weekday_dates, group_slots_by_day, time_to_mins
from app.services.google.auth import get_oauth2_credentials
from app.services.google.calendar import create_weekly_class_events, update_weekly_class_events
from app.services.google.cleanup import delete_student_google
from app.services.google.drive import create_student_drive_folder, update_student_meet_doc
from app.services.google.sync import sync_all_students
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
    if hasattr(result, "error") and result.error:
        return {"error": result.error.message}
    if not result.data:
        return {"error": "Student not found"}
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
    if (hasattr(fetch_result, "error") and fetch_result.error) or not fetch_result.data:
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
    insert_data = {
        "name": params["name"],
        "mode": params["mode"],
        "fee_per_hour": params["fee_per_hour"],
        "payment_method": params.get("payment_method", "Monthly"),
        "status": params.get("status", "Active"),
        "class_schedule": params.get("class_schedule") or [],
        "contact_person": params.get("contact_person"),
        "contact_phone": params.get("contact_phone"),
        "student_phone": params.get("student_phone"),
        "today_homework": params.get("today_homework"),
        "notes": params.get("notes"),
        "latest_payment": params.get("latest_payment"),
        "access_emails": params.get("access_emails") or [],
        "google_meet_link": params.get("google_meet_link"),
        "google_drive_link": params.get("google_drive_link"),
    }

    result = (
        await supabase.from_("students")
        .insert(insert_data)
        .select("id, name")
        .single()
        .execute()
    )
    if hasattr(result, "error") and result.error:
        return {"error": result.error.message}

    suggest = bool(params.get("class_schedule"))
    response: dict = {"student": result.data}
    if suggest:
        response["suggestGoogleSetup"] = True
    return response


async def update_student(supabase: AsyncClient, id: str, fields: dict) -> dict:
    permitted = {k: v for k, v in fields.items() if k in ALLOWED_UPDATE_KEYS}
    if not permitted:
        return {"error": "No valid fields to update"}

    if "access_emails" in permitted:
        permitted["access_emails"] = [e.strip().lower() for e in (permitted["access_emails"] or [])]

    update_result = (
        await supabase.from_("students")
        .update(permitted)
        .eq("id", id)
        .execute()
    )
    if hasattr(update_result, "error") and update_result.error:
        return {"error": update_result.error.message}

    if "class_schedule" not in permitted:
        return {"success": True}

    # Fetch Google integration fields to check if sync is needed
    fetch_result = (
        await supabase.from_("students")
        .select("name, calendar_event_ids, google_meet_link, google_drive_link")
        .eq("id", id)
        .maybe_single()
        .execute()
    )
    student = fetch_result.data if fetch_result and fetch_result.data else None

    if not student or not student.get("calendar_event_ids") or not student.get("google_meet_link"):
        return {"success": True, "suggestGoogleSetup": True}

    try:
        creds = await get_oauth2_credentials(supabase)
    except Exception as err:
        return {
            "success": True,
            "googleWarnings": [f"Schedule saved but Calendar not updated: {err_msg(err, 'Google not connected')}"],
        }

    warnings: list[str] = []
    new_schedule = [ClassSlot(**s) for s in (permitted["class_schedule"] or [])]

    cal_task = update_weekly_class_events(
        creds,
        student["name"],
        new_schedule,
        student["calendar_event_ids"],
        student["google_meet_link"],
    )
    drive_link = student.get("google_drive_link")
    drive_task = (
        update_student_meet_doc(
            creds,
            drive_link,
            student["name"],
            new_schedule,
            student["google_meet_link"],
        )
        if drive_link
        else asyncio.sleep(0)
    )

    cal_result, drive_result = await asyncio.gather(cal_task, drive_task, return_exceptions=True)

    if isinstance(cal_result, Exception):
        warnings.append(f"Calendar update failed: {err_msg(cal_result)}")
    else:
        new_event_ids = cal_result["event_ids"]  # type: ignore[index]
        new_meet_link: str | None = cal_result.get("meet_link")  # type: ignore[union-attr]

        db_update: dict = {"calendar_event_ids": new_event_ids}
        if new_meet_link:
            db_update["google_meet_link"] = new_meet_link
        db_save = await supabase.from_("students").update(db_update).eq("id", id).execute()
        if hasattr(db_save, "error") and db_save.error:
            warnings.append(f"Calendar updated but DB save failed: {db_save.error.message}")

        if new_meet_link and drive_link:
            try:
                await update_student_meet_doc(
                    creds, drive_link, student["name"], new_schedule, new_meet_link
                )
            except Exception as err:
                warnings.append(f"Drive Meet doc update failed: {err_msg(err)}")

    new_meet_link_generated = not isinstance(cal_result, Exception) and bool(
        cal_result.get("meet_link")  # type: ignore[union-attr]
    )
    if isinstance(drive_result, Exception) and not new_meet_link_generated:
        warnings.append(f"Drive Meet doc update failed: {err_msg(drive_result)}")

    response: dict = {"success": True}
    if warnings:
        response["googleWarnings"] = warnings
    return response


async def delete_student(supabase: AsyncClient, id: str) -> dict:
    fetch_result = (
        await supabase.from_("students")
        .select("google_drive_link, calendar_event_ids")
        .eq("id", id)
        .maybe_single()
        .execute()
    )
    student = fetch_result.data if fetch_result and fetch_result.data else None

    warnings: list[str] = []

    if student and (student.get("google_drive_link") or student.get("calendar_event_ids")):
        try:
            creds = await get_oauth2_credentials(supabase)
            google_result = await delete_student_google(
                creds,
                student.get("google_drive_link"),
                student.get("calendar_event_ids"),
            )
            if google_result.get("drive_error"):
                warnings.append(f"Drive cleanup warning: {google_result['drive_error']}")
            if google_result.get("calendar_error"):
                warnings.append(f"Calendar cleanup warning: {google_result['calendar_error']}")
        except Exception as err:
            warnings.append(f"Google cleanup skipped: {err_msg(err, 'auth error')}")

    delete_result = (
        await supabase.from_("students")
        .delete()
        .eq("id", id)
        .execute()
    )
    if hasattr(delete_result, "error") and delete_result.error:
        return {"error": delete_result.error.message}

    response: dict = {"success": True}
    if warnings:
        response["warnings"] = warnings
    return response


async def setup_student_google(supabase: AsyncClient, student_id: str) -> dict:
    fetch_result = (
        await supabase.from_("students")
        .select("name, mode, class_schedule, calendar_event_ids, google_meet_link, google_drive_link")
        .eq("id", student_id)
        .single()
        .execute()
    )
    if (hasattr(fetch_result, "error") and fetch_result.error) or not fetch_result.data:
        return {"error": "Student not found"}

    student = fetch_result.data
    name = student["name"]
    mode = student["mode"]
    class_schedule = student.get("class_schedule") or []
    calendar_event_ids = student.get("calendar_event_ids") or []
    google_meet_link: str | None = student.get("google_meet_link")
    google_drive_link: str | None = student.get("google_drive_link")

    if not class_schedule:
        return {"error": "Student has no class schedule — add a schedule before setting up Google."}

    needs_calendar = not calendar_event_ids
    needs_drive = not google_drive_link

    if not needs_calendar and not needs_drive:
        return {"result": "Already fully set up — Calendar ✓, Drive ✓. Nothing to do."}

    try:
        creds = await get_oauth2_credentials(supabase)
    except Exception as err:
        return {"error": err_msg(err, "Google not connected")}

    summary: list[str] = []
    slots = [ClassSlot(**s) for s in class_schedule]

    if needs_calendar:
        try:
            cal_result = await create_weekly_class_events(creds, name, slots)
            meet_link = cal_result["meet_link"]
            event_ids = cal_result["event_ids"]
            cal_save = (
                await supabase.from_("students")
                .update({"google_meet_link": meet_link, "calendar_event_ids": event_ids})
                .eq("id", student_id)
                .execute()
            )
            if hasattr(cal_save, "error") and cal_save.error:
                return {"error": f"Calendar events created but DB save failed: {cal_save.error.message}"}
            google_meet_link = meet_link
            count = len(event_ids)
            summary.append(f"Calendar ✓ ({count} event{'s' if count != 1 else ''} created, Meet link saved)")
        except Exception as err:
            return {"error": f"Calendar setup failed: {err_msg(err)}"}
    else:
        summary.append("Calendar ✓ (already set up, skipped)")

    if needs_drive:
        if not google_meet_link:
            return {"error": "No Meet link available — Calendar setup must succeed before Drive can be created."}
        try:
            drive_url = await create_student_drive_folder(
                creds, name, google_meet_link, slots, mode
            )
            drive_save = (
                await supabase.from_("students")
                .update({"google_drive_link": drive_url})
                .eq("id", student_id)
                .execute()
            )
            if hasattr(drive_save, "error") and drive_save.error:
                summary.append(f"Drive ✗ (folder created but DB save failed: {drive_save.error.message})")
            else:
                summary.append("Drive ✓ (folder created)")
        except Exception as err:
            summary.append(f"Drive ✗ ({err_msg(err)})")
    else:
        summary.append("Drive ✓ (already set up, skipped)")

    return {"result": ", ".join(summary)}


async def run_sync_all(supabase: AsyncClient) -> dict:
    try:
        creds = await get_oauth2_credentials(supabase)
        results = await sync_all_students(supabase, creds)
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
