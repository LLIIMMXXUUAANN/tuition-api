import asyncio
from functools import partial

from google.oauth2.credentials import Credentials

from app.config import settings
from app.services.google.calendar import _delete_event
from app.services.google.drive import _session, _trash_file, parse_drive_folder_id


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
    loop = asyncio.get_running_loop()

    async def _trash_drive() -> None:
        if not drive_url or not drive_url.strip():
            return
        folder_id = parse_drive_folder_id(drive_url)
        session = _session(creds)
        await loop.run_in_executor(None, partial(_trash_file, session, folder_id))

    async def _delete_calendar() -> None:
        if not event_ids:
            return
        calendar_id = settings.google_calendar_id

        async def _delete_one(event_id: str) -> str | None:
            try:
                await loop.run_in_executor(
                    None, partial(_delete_event, creds, calendar_id, event_id)
                )
                return None
            except Exception as exc:
                if "404" in str(exc) or "410" in str(exc):
                    return None  # already deleted — desired outcome
                return event_id

        results = await asyncio.gather(*[_delete_one(eid) for eid in event_ids if eid])
        failed = [r for r in results if r is not None]
        if failed:
            raise RuntimeError(f"Failed to delete calendar events: {', '.join(failed)}")

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
