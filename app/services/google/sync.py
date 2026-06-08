import asyncio

from google.oauth2.credentials import Credentials
from supabase import AsyncClient

from app.services.google.calendar import find_recurring_event_ids, update_weekly_class_events
from app.services.google.drive import update_student_meet_doc
from app.types import ClassSlot


def _err_msg(exc: object, fallback: str) -> str:
    return str(exc) if exc and str(exc) else fallback


def _auth_expired(msg: str) -> bool:
    return "invalid_grant" in msg


async def sync_all_students(supabase: AsyncClient, creds: Credentials) -> list[dict]:
    """
    Syncs all active students' Google Calendar events and Drive Meet docs to
    match the DB schedule. Always calls find_recurring_event_ids per student
    and merges with DB ids to catch rogue events.
    Returns a list of {"name", "status", "reason"} dicts.
    """
    result = (
        await supabase.from_("students")
        .select(
            "id, name, class_schedule, calendar_event_ids, google_meet_link, google_drive_link"
        )
        .eq("status", "Active")
        .order("name")
        .execute()
    )
    students = result.data or []

    async def _sync_student(student: dict) -> dict:
        sid = student["id"]
        name = student["name"]
        class_schedule = student.get("class_schedule") or []
        calendar_event_ids = student.get("calendar_event_ids") or []
        google_meet_link = student.get("google_meet_link")
        google_drive_link = student.get("google_drive_link")

        if not class_schedule:
            return {"name": name, "status": "skipped", "reason": "no class schedule"}
        if not google_meet_link:
            return {"name": name, "status": "skipped", "reason": "no Meet link"}

        db_ids: list[str] = calendar_event_ids or []
        search_ids: list[str] = []
        try:
            search_ids = await find_recurring_event_ids(creds, name)
        except Exception as exc:
            raw = _err_msg(exc, "Calendar search failed")
            if _auth_expired(raw):
                return {
                    "name": name,
                    "status": "error",
                    "reason": "Google auth expired — reconnect",
                }
            # Non-fatal: fall back to DB IDs only

        event_ids = list(dict.fromkeys(db_ids + search_ids))  # deduplicated, order-preserving
        if not event_ids:
            return {
                "name": name,
                "status": "skipped",
                "reason": "no Calendar events found — use Create Calendar Event",
            }

        slots = [ClassSlot(**s) for s in class_schedule]

        try:
            cal_result, drive_result = await asyncio.gather(
                update_weekly_class_events(creds, name, slots, event_ids, google_meet_link),
                update_student_meet_doc(
                    creds, google_drive_link, name, slots, google_meet_link
                )
                if google_drive_link
                else asyncio.sleep(0),
                return_exceptions=True,
            )

            if isinstance(cal_result, Exception):
                raw = _err_msg(cal_result, "Calendar update failed")
                return {
                    "name": name,
                    "status": "error",
                    "reason": "Google auth expired — reconnect" if _auth_expired(raw) else raw,
                }

            new_event_ids = cal_result["event_ids"]  # type: ignore[index]
            new_meet_link: str | None = cal_result.get("meet_link")  # type: ignore[union-attr]

            db_update: dict = {"calendar_event_ids": new_event_ids}
            if new_meet_link:
                db_update["google_meet_link"] = new_meet_link
            await supabase.from_("students").update(db_update).eq("id", sid).execute()

            # Primary was regenerated — re-update Drive doc with the new Meet link
            drive_update_error: str | None = None
            if new_meet_link and google_drive_link:
                try:
                    await update_student_meet_doc(
                        creds, google_drive_link, name, slots, new_meet_link
                    )
                except Exception as exc:
                    drive_update_error = _err_msg(exc, "failed")

            notes_parts = []
            if not calendar_event_ids:
                notes_parts.append("IDs found via search")
            if new_meet_link:
                notes_parts.append("new Meet link generated (primary was missing)")
            if drive_update_error:
                notes_parts.append(f"Drive doc (new link): {drive_update_error}")
            elif isinstance(drive_result, Exception) and not new_meet_link:
                notes_parts.append(f"Drive doc: {_err_msg(drive_result, 'failed')}")

            notes = " · ".join(notes_parts) if notes_parts else None
            return {"name": name, "status": "synced", "reason": notes}

        except Exception as exc:
            return {"name": name, "status": "error", "reason": _err_msg(exc, "Unknown error")}

    return await asyncio.gather(*[_sync_student(s) for s in students])  # type: ignore[return-value]
