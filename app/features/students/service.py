"""Student business logic — create/update/delete with automatic Google sync."""

from __future__ import annotations

import asyncio

from supabase import AsyncClient

from app.features.google.auth import get_oauth2_credentials, save_token_if_rotated
from app.features.google.calendar import (
    create_weekly_class_events,
    find_recurring_event_ids,
    update_weekly_class_events,
)
from app.features.google.cleanup import delete_student_google
from app.features.google.drive import create_student_drive_folder, update_student_meet_doc
from app.types import ClassSlot


class StudentNotFoundError(Exception):
    pass


def _google_err(exc: Exception) -> str:
    raw = str(exc)
    if "invalid_grant" in raw:
        return "Google auth expired. Visit /api/google/auth to reconnect."
    if "insufficient" in raw.lower() or "403" in raw:
        return "Google API not authorised. Visit /api/google/auth to re-connect."
    return raw


async def create_student(supabase: AsyncClient, data: dict) -> dict:
    """Insert a student row then auto-setup Google Calendar + Drive.

    Returns {"id": str, "name": str, "google_warning": str | None}.
    Raises Exception on DB failure.
    """
    insert_data = {
        "name": data["name"],
        "mode": data["mode"],
        "fee_per_hour": data["fee_per_hour"],
        "payment_method": data.get("payment_method", "Monthly"),
        "status": data.get("status", "Active"),
        "class_schedule": [
            s.model_dump() if hasattr(s, "model_dump") else s
            for s in (data.get("class_schedule") or [])
        ],
        "contact_person": data.get("contact_person"),
        "contact_phone": data.get("contact_phone"),
        "student_phone": data.get("student_phone"),
        "today_homework": data.get("today_homework"),
        "notes": data.get("notes"),
        "latest_payment": data.get("latest_payment"),
        "access_emails": data.get("access_emails") or [],
        "google_meet_link": data.get("google_meet_link"),
        "google_drive_link": data.get("google_drive_link"),
    }

    result = (
        await supabase.from_("students")
        .insert(insert_data)
        .select("id, name")
        .single()
        .execute()
    )
    if hasattr(result, "error") and result.error:
        raise Exception(result.error.message)
    if not result.data:
        raise Exception("Insert returned no data")

    student_id: str = result.data["id"]
    student_name: str = result.data["name"]
    class_schedule: list[dict] = insert_data["class_schedule"]

    google_warning: str | None = None
    try:
        creds, stored_token = await get_oauth2_credentials(supabase)
        meet_link = ""
        event_ids: list[str] = []

        if class_schedule:
            slots = [ClassSlot(**s) for s in class_schedule]
            cal = await create_weekly_class_events(creds, student_name, slots)
            meet_link = cal["meet_link"]
            event_ids = cal["event_ids"]

        resolved_mode = (
            "Other Syllabus" if data.get("mode") == "Other Syllabus" else "My Python Syllabus"
        )
        drive_url = await create_student_drive_folder(
            creds,
            student_name,
            meet_link,
            [ClassSlot(**s) for s in class_schedule],
            resolved_mode,
        )
        await save_token_if_rotated(creds, stored_token, supabase)
        await supabase.from_("students").update({
            "google_meet_link": meet_link or None,
            "google_drive_link": drive_url,
            "calendar_event_ids": event_ids,
        }).eq("id", student_id).execute()
    except Exception as exc:
        google_warning = _google_err(exc)

    return {"id": student_id, "name": student_name, "google_warning": google_warning}


async def update_student(supabase: AsyncClient, student_id: str, fields: dict) -> dict:
    """Update student fields and sync Google Calendar/Drive if schedule changed.

    Returns {"ok": True, "google_warning": str | None}.
    Raises StudentNotFoundError if student does not exist.
    Raises Exception on DB failure.
    """
    fetch = (
        await supabase.from_("students")
        .select("*")
        .eq("id", student_id)
        .maybe_single()
        .execute()
    )
    if not fetch.data:
        raise StudentNotFoundError(student_id)
    current = fetch.data

    update_data = dict(fields)

    if "access_emails" in update_data:
        update_data["access_emails"] = [
            e.strip().lower() for e in (update_data["access_emails"] or [])
        ]

    if "class_schedule" in update_data:
        update_data["class_schedule"] = [
            s.model_dump() if hasattr(s, "model_dump") else s
            for s in (update_data["class_schedule"] or [])
        ]

    google_warning: str | None = None

    if "class_schedule" in update_data:
        new_schedule_raw: list[dict] = update_data["class_schedule"]
        old_schedule = current.get("class_schedule") or []

        if new_schedule_raw != old_schedule:
            student_name: str = update_data.get("name") or current["name"]
            existing_ids: list[str] = current.get("calendar_event_ids") or []
            current_meet_link: str = current.get("google_meet_link") or ""
            drive_url: str | None = current.get("google_drive_link") or None

            try:
                creds, stored_token = await get_oauth2_credentials(supabase)

                if not new_schedule_raw:
                    # Branch 1: Schedule cleared — delete events, blank Drive doc
                    search_ids, drive_res = await asyncio.gather(
                        find_recurring_event_ids(creds, student_name),
                        update_student_meet_doc(
                            creds, drive_url, student_name, [], current_meet_link
                        ) if drive_url else asyncio.sleep(0),
                        return_exceptions=True,
                    )
                    merged = list(dict.fromkeys(
                        existing_ids + (search_ids if not isinstance(search_ids, Exception) else [])
                    ))
                    if merged:
                        await update_weekly_class_events(
                            creds, student_name, [], merged, current_meet_link
                        )
                    if isinstance(drive_res, Exception):
                        google_warning = f"Drive doc not blanked: {drive_res}"
                    update_data["calendar_event_ids"] = []
                    update_data["google_meet_link"] = None

                elif existing_ids and current_meet_link:
                    # Branch 2: Update existing events (nuke-and-repave)
                    new_schedule = [ClassSlot(**s) for s in new_schedule_raw]
                    search_ids, drive_res = await asyncio.gather(
                        find_recurring_event_ids(creds, student_name),
                        update_student_meet_doc(
                            creds, drive_url, student_name, new_schedule, current_meet_link
                        ) if drive_url else asyncio.sleep(0),
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
                    # Branch 3: First time — create Calendar events, update Drive doc
                    new_schedule = [ClassSlot(**s) for s in new_schedule_raw]
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

            except StudentNotFoundError:
                raise
            except Exception as exc:
                google_warning = _google_err(exc)

    result = (
        await supabase.from_("students")
        .update(update_data)
        .eq("id", student_id)
        .execute()
    )
    if hasattr(result, "error") and result.error:
        raise Exception(result.error.message)

    return {"ok": True, "google_warning": google_warning}


async def delete_student(supabase: AsyncClient, student_id: str) -> dict:
    """Clean up Google resources then hard-delete the student row.

    Returns {"ok": True, "google_warning": str | None}.
    Raises StudentNotFoundError if student does not exist.
    Raises Exception on DB failure.
    """
    fetch = (
        await supabase.from_("students")
        .select("google_drive_link, calendar_event_ids")
        .eq("id", student_id)
        .maybe_single()
        .execute()
    )
    if not fetch.data:
        raise StudentNotFoundError(student_id)
    student = fetch.data

    google_warning: str | None = None
    drive_url: str | None = student.get("google_drive_link")
    event_ids: list[str] = student.get("calendar_event_ids") or []

    if drive_url or event_ids:
        try:
            creds, stored_token = await get_oauth2_credentials(supabase)
            google_result = await delete_student_google(creds, drive_url, event_ids)
            await save_token_if_rotated(creds, stored_token, supabase)
            errors = []
            if google_result.get("drive_error"):
                errors.append(f"Drive cleanup: {google_result['drive_error']}")
            if google_result.get("calendar_error"):
                errors.append(f"Calendar cleanup: {google_result['calendar_error']}")
            if errors:
                google_warning = "; ".join(errors)
        except Exception as exc:
            google_warning = f"Google cleanup skipped: {_google_err(exc)}"

    result = (
        await supabase.from_("students")
        .delete()
        .eq("id", student_id)
        .execute()
    )
    if hasattr(result, "error") and result.error:
        raise Exception(result.error.message)

    return {"ok": True, "google_warning": google_warning}
