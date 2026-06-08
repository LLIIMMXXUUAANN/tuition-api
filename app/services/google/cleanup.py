import asyncio

from google.oauth2.credentials import Credentials

from app.config import settings
from app.services.google.auth import build_calendar, build_drive
from app.services.google.drive import parse_drive_folder_id


async def delete_student_google(
    creds: Credentials,
    drive_url: str | None = None,
    event_ids: list[str] | None = None,
) -> dict:
    """
    Trashes the student's Drive folder and deletes their Calendar events.
    Both operations are non-fatal — errors are captured and returned.
    Returns {"drive_error": str|None, "calendar_error": str|None}.
    """
    loop = asyncio.get_event_loop()

    async def _trash_drive() -> None:
        if not drive_url or not drive_url.strip():
            return
        folder_id = parse_drive_folder_id(drive_url)
        service = build_drive(creds)
        await loop.run_in_executor(
            None,
            lambda: service.files()
            .update(fileId=folder_id, body={"trashed": True})
            .execute(),
        )

    async def _delete_calendar() -> None:
        if not event_ids:
            return
        calendar_id = settings.google_calendar_id
        service = build_calendar(creds)

        async def _delete_one(event_id: str) -> None:
            try:
                await loop.run_in_executor(
                    None,
                    lambda: service.events()
                    .delete(calendarId=calendar_id, eventId=event_id)
                    .execute(),
                )
            except Exception as exc:
                print(f"Failed to delete calendar event {event_id}: {exc}")

        await asyncio.gather(*[_delete_one(eid) for eid in event_ids if eid])

    drive_result, calendar_result = await asyncio.gather(
        _trash_drive(),
        _delete_calendar(),
        return_exceptions=True,
    )

    def _err_str(result: object, fallback: str) -> str | None:
        if isinstance(result, Exception):
            return str(result) if str(result) else fallback
        return None

    return {
        "drive_error": _err_str(drive_result, "Failed to trash Drive folder"),
        "calendar_error": _err_str(calendar_result, "Failed to delete Calendar events"),
    }
