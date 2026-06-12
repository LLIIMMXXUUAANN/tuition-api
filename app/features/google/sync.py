import asyncio

from google.oauth2.credentials import Credentials
from supabase import AsyncClient

from app.features.google.calendar import (
    create_weekly_class_events,
    find_recurring_event_ids,
    update_weekly_class_events,
)
from app.features.google.drive import create_student_drive_folder, update_student_meet_doc
from app.types import ClassSlot


def _err_msg(exc: object, fallback: str) -> str:
    return str(exc) if exc and str(exc) else fallback


def _auth_expired(msg: str) -> bool:
    return "invalid_grant" in msg


async def sync_all_students(supabase: AsyncClient, creds: Credentials) -> list[dict]:
    """
    Syncs all active students' Google Calendar events and Drive Meet docs to
    match the DB schedule. Creates missing Calendar events and Drive folders;
    recovers existing Meet links from Calendar when DB has none.
    Returns a list of {"name", "status", "reason"} dicts.
    """
    result = (
        await supabase.from_("students")
        .select(
            "id, name, mode, class_schedule, calendar_event_ids, google_meet_link, google_drive_link"
        )
        .eq("status", "Active")
        .order("name")
        .execute()
    )
    students = result.data or []

    async def _sync_student(student: dict) -> dict:
        sid = student["id"]
        name = student["name"]
        mode: str = student.get("mode") or "My Python Syllabus"
        class_schedule = student.get("class_schedule") or []
        calendar_event_ids = student.get("calendar_event_ids") or []
        google_meet_link: str | None = student.get("google_meet_link")
        google_drive_link: str | None = student.get("google_drive_link")

        if not class_schedule:
            return {"name": name, "status": "skipped", "reason": "no class schedule"}

        # Step 0: search Calendar for any existing events + merge with DB IDs
        db_ids: list[str] = calendar_event_ids or []
        search_ids: list[str] = []
        try:
            search_ids = await find_recurring_event_ids(creds, name)
        except Exception as exc:
            raw = _err_msg(exc, "Calendar search failed")
            if _auth_expired(raw):
                return {"name": name, "status": "error", "reason": "Google auth expired — reconnect"}
            # Non-fatal: fall back to DB IDs only

        event_ids = list(dict.fromkeys(db_ids + search_ids))
        slots = [ClassSlot(**s) for s in class_schedule]

        # Step 1: Calendar — update (nuke-and-repave) if we have event IDs, else create fresh
        new_meet_link: str | None = None
        effective_link: str | None = None

        try:
            if event_ids:
                cal_result = await update_weekly_class_events(
                    creds, name, slots, event_ids, google_meet_link
                )
                new_event_ids: list[str] = cal_result["event_ids"]
                new_meet_link = cal_result.get("meet_link")
                effective_link = (
                    cal_result.get("effective_meet_link") or new_meet_link or google_meet_link
                )
            else:
                cal_result = await create_weekly_class_events(creds, name, slots)
                new_event_ids = cal_result["event_ids"]
                new_meet_link = cal_result["meet_link"]
                effective_link = new_meet_link
        except Exception as exc:
            raw = _err_msg(exc, "Calendar update failed")
            return {
                "name": name,
                "status": "error",
                "reason": "Google auth expired — reconnect" if _auth_expired(raw) else raw,
            }

        # Step 2: persist updated Calendar IDs; update Meet link only if it changed
        db_update: dict = {"calendar_event_ids": new_event_ids}
        if effective_link and effective_link != google_meet_link:
            db_update["google_meet_link"] = effective_link
        await supabase.from_("students").update(db_update).eq("id", sid).execute()

        # Step 3: Drive — create folder if missing, update Meet doc if folder exists
        drive_error: str | None = None
        drive_created = False

        if effective_link and not google_drive_link:
            try:
                new_drive_url = await create_student_drive_folder(
                    creds, name, effective_link, slots, mode
                )
                await supabase.from_("students").update(
                    {"google_drive_link": new_drive_url}
                ).eq("id", sid).execute()
                drive_created = True
            except Exception as exc:
                raw = _err_msg(exc, "Drive folder creation failed")
                drive_error = "Google auth expired — reconnect" if _auth_expired(raw) else raw
        elif google_drive_link and effective_link:
            try:
                await update_student_meet_doc(
                    creds, google_drive_link, name, slots, effective_link
                )
            except Exception as exc:
                raw = _err_msg(exc, "Drive doc update failed")
                drive_error = "Google auth expired — reconnect" if _auth_expired(raw) else raw

        # Build status notes
        notes_parts: list[str] = []
        if not calendar_event_ids and not event_ids:
            notes_parts.append("Calendar events created")
        elif not calendar_event_ids and search_ids:
            notes_parts.append("IDs found via search")
        if new_meet_link:
            notes_parts.append("new Meet link generated")
        elif effective_link and not google_meet_link:
            notes_parts.append("Meet link recovered from Calendar")
        if drive_created:
            notes_parts.append("Drive folder created")
        if drive_error:
            notes_parts.append(f"Drive: {drive_error}")

        notes = " · ".join(notes_parts) if notes_parts else None
        return {"name": name, "status": "synced", "reason": notes}

    return await asyncio.gather(*[_sync_student(s) for s in students])  # type: ignore[return-value]
