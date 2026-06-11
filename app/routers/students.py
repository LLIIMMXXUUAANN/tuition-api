"""Student CRUD endpoints — handles writes that were previously in browser Supabase client."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import require_internal_secret
from app.services.google.auth import get_oauth2_credentials, save_token_if_rotated
from app.services.google.calendar import (
    create_weekly_class_events,
    find_recurring_event_ids,
    update_weekly_class_events,
)
from app.services.google.cleanup import delete_student_google
from app.services.google.drive import create_student_drive_folder, update_student_meet_doc
from app.services.supabase_client import get_supabase
from app.types import ClassSlot

router = APIRouter(dependencies=[Depends(require_internal_secret)])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class StudentPayload(BaseModel):
    name: str
    mode: str
    fee_per_hour: float
    payment_method: str = "Monthly"
    status: str = "Active"
    class_schedule: list[ClassSlot] | None = None
    contact_person: str | None = None
    contact_phone: str | None = None
    student_phone: str | None = None
    today_homework: str | None = None
    notes: str | None = None
    latest_payment: str | None = None
    access_emails: list[str] | None = None


class StudentUpdatePayload(BaseModel):
    name: str | None = None
    mode: str | None = None
    fee_per_hour: float | None = None
    payment_method: str | None = None
    status: str | None = None
    class_schedule: list[ClassSlot] | None = None
    contact_person: str | None = None
    contact_phone: str | None = None
    student_phone: str | None = None
    today_homework: str | None = None
    notes: str | None = None
    latest_payment: str | None = None
    access_emails: list[str] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _google_err(exc: Exception) -> str:
    raw = str(exc)
    if "invalid_grant" in raw:
        return "Google auth expired. Visit /api/google/auth to reconnect."
    if "insufficient" in raw.lower() or "403" in raw:
        return "Google API not authorised. Visit /api/google/auth to re-connect."
    return raw


# ---------------------------------------------------------------------------
# Student endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_students(status: str | None = None):
    supabase = await get_supabase()
    query = supabase.from_("students").select("*").order("name")
    if status:
        query = query.eq("status", status)
    result = await query.execute()
    return result.data or []


@router.get("/portal-lookup")
async def portal_lookup(email: str):
    supabase = await get_supabase()
    result = (
        await supabase.from_("students")
        .select("*")
        .contains("access_emails", [email])
        .limit(1)
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Student not found")
    return result.data


@router.get("/{student_id}")
async def get_student(student_id: str):
    supabase = await get_supabase()
    result = (
        await supabase.from_("students")
        .select("*")
        .eq("id", student_id)
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Student not found")
    return result.data


@router.post("", status_code=201)
async def create_student(body: StudentPayload):
    supabase = await get_supabase()
    data = body.model_dump()
    data["class_schedule"] = [
        s.model_dump() if hasattr(s, "model_dump") else s
        for s in (body.class_schedule or [])
    ]

    result = await supabase.from_("students").insert(data).execute()
    if hasattr(result, "error") and result.error:
        raise HTTPException(status_code=400, detail=result.error.message)
    if not result.data:
        raise HTTPException(status_code=500, detail="Insert returned no data")
    student_id = result.data[0]["id"]

    # Drive folder always created (blank doc if no schedule). Calendar events only if schedule.
    google_warning: str | None = None
    try:
        creds, stored_token = await get_oauth2_credentials(supabase)
        meet_link = ""
        event_ids: list[str] = []

        if body.class_schedule:
            cal = await create_weekly_class_events(creds, body.name, body.class_schedule)
            meet_link = cal["meet_link"]
            event_ids = cal["event_ids"]

        resolved_mode = "Other Syllabus" if body.mode == "Other Syllabus" else "My Python Syllabus"
        drive_url = await create_student_drive_folder(
            creds, body.name, meet_link, body.class_schedule or [], resolved_mode
        )
        await save_token_if_rotated(creds, stored_token, supabase)
        await supabase.from_("students").update({
            "google_meet_link": meet_link or None,
            "google_drive_link": drive_url,
            "calendar_event_ids": event_ids,
        }).eq("id", student_id).execute()
    except Exception as exc:
        google_warning = _google_err(exc)

    return {"id": student_id, "google_warning": google_warning}


@router.put("/{student_id}")
async def update_student(student_id: str, body: StudentUpdatePayload):
    supabase = await get_supabase()
    update_data = body.model_dump(exclude_unset=True)

    if not update_data:
        raise HTTPException(status_code=400, detail="No fields provided")

    fetch = (
        await supabase.from_("students").select("*").eq("id", student_id).maybe_single().execute()
    )
    if not fetch.data:
        raise HTTPException(status_code=404, detail="Student not found")
    current = fetch.data

    google_warning: str | None = None

    if "class_schedule" in update_data:
        new_schedule: list[ClassSlot] = body.class_schedule or []
        old_schedule = current.get("class_schedule") or []
        new_serialized = [s.model_dump() if hasattr(s, "model_dump") else s for s in new_schedule]
        update_data["class_schedule"] = new_serialized

        if new_serialized != old_schedule:
            student_name: str = update_data.get("name") or current["name"]
            existing_ids: list[str] = current.get("calendar_event_ids") or []
            current_meet_link: str = current.get("google_meet_link") or ""
            drive_url: str | None = current.get("google_drive_link") or None
            current_mode: str = update_data.get("mode") or current.get("mode", "My Python Syllabus")

            try:
                creds, stored_token = await get_oauth2_credentials(supabase)

                if not new_schedule:
                    # Clear: delete Calendar events + blank Drive doc
                    search_ids, drive_res = await asyncio.gather(
                        find_recurring_event_ids(creds, student_name),
                        update_student_meet_doc(creds, drive_url, student_name, [], current_meet_link)
                        if drive_url else asyncio.sleep(0),
                        return_exceptions=True,
                    )
                    merged = list(dict.fromkeys(
                        existing_ids + (search_ids if not isinstance(search_ids, Exception) else [])
                    ))
                    if merged:
                        await update_weekly_class_events(creds, student_name, [], merged, current_meet_link)
                    if isinstance(drive_res, Exception):
                        google_warning = f"Drive doc not blanked: {drive_res}"
                    update_data["calendar_event_ids"] = []
                    update_data["google_meet_link"] = None

                elif existing_ids and current_meet_link:
                    # Update: nuke-and-repave Calendar + update Drive doc
                    search_ids, drive_res = await asyncio.gather(
                        find_recurring_event_ids(creds, student_name),
                        update_student_meet_doc(creds, drive_url, student_name, new_schedule, current_meet_link)
                        if drive_url else asyncio.sleep(0),
                        return_exceptions=True,
                    )
                    merged = list(dict.fromkeys(
                        existing_ids + (search_ids if not isinstance(search_ids, Exception) else [])
                    ))
                    cal = await update_weekly_class_events(
                        creds, student_name, new_schedule, merged, current_meet_link
                    )
                    new_meet_link = cal.get("meet_link")
                    if new_meet_link and drive_url:
                        try:
                            await update_student_meet_doc(
                                creds, drive_url, student_name, new_schedule, new_meet_link
                            )
                            drive_res = None
                        except Exception as exc:
                            drive_res = exc
                    update_data["calendar_event_ids"] = cal["event_ids"]
                    if new_meet_link:
                        update_data["google_meet_link"] = new_meet_link
                    if isinstance(drive_res, Exception):
                        google_warning = f"Drive doc not updated: {drive_res}"

                else:
                    # Schedule added for first time — Drive folder already exists.
                    # Create Calendar events + update the existing Drive doc.
                    cal = await create_weekly_class_events(creds, student_name, new_schedule)
                    meet_link = cal["meet_link"]
                    if drive_url:
                        try:
                            await update_student_meet_doc(
                                creds, drive_url, student_name, new_schedule, meet_link
                            )
                        except Exception as exc:
                            google_warning = f"Drive doc not updated: {exc}"
                    update_data["google_meet_link"] = meet_link
                    update_data["calendar_event_ids"] = cal["event_ids"]

                await save_token_if_rotated(creds, stored_token, supabase)

            except Exception as exc:
                google_warning = _google_err(exc)

    result = await supabase.from_("students").update(update_data).eq("id", student_id).execute()
    if hasattr(result, "error") and result.error:
        raise HTTPException(status_code=400, detail=result.error.message)
    return {"ok": True, "google_warning": google_warning}


@router.delete("/{student_id}")
async def delete_student(student_id: str):
    supabase = await get_supabase()

    # Fetch Google resource references before deleting the row
    fetch = (
        await supabase.from_("students")
        .select("google_drive_link, calendar_event_ids")
        .eq("id", student_id)
        .maybe_single()
        .execute()
    )
    if not fetch.data:
        raise HTTPException(status_code=404, detail="Student not found")

    drive_url: str | None = fetch.data.get("google_drive_link")
    event_ids: list[str] = fetch.data.get("calendar_event_ids") or []

    # Clean up Google resources if any exist (non-fatal — errors are returned, not raised)
    google_errors: dict = {}
    if drive_url or event_ids:
        try:
            creds, stored_token = await get_oauth2_credentials(supabase)
            google_errors = await delete_student_google(creds, drive_url, event_ids)
            await save_token_if_rotated(creds, stored_token, supabase)
        except Exception as exc:
            google_errors = {"google_error": str(exc)}

    result = await supabase.from_("students").delete().eq("id", student_id).execute()
    if hasattr(result, "error") and result.error:
        raise HTTPException(status_code=400, detail=result.error.message)
    return {"ok": True, **google_errors}
