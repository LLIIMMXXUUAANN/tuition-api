import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.auth import require_internal_secret
from app.shared.response_models import (
    CreateClassEventResponse,
    CreateStudentFolderResponse,
    DeleteStudentGoogleResponse,
    GoogleCallbackResponse,
    OkResponse,
    SyncAllResponse,
    UpdateClassEventResponse,
)
from app.features.google.auth import (
    build_google_auth_url,
    exchange_code_for_refresh_token,
    generate_state_token,
    get_oauth2_credentials,
    save_token_if_rotated,
    verify_and_consume_state,
)
from app.features.google.calendar import (
    create_weekly_class_events,
    find_recurring_event_ids,
    update_weekly_class_events,
)
from app.features.google.cleanup import delete_student_google
from app.features.google.drive import (
    create_student_drive_folder,
    update_student_meet_doc,
)
from app.features.google.sync import sync_all_students
from app.shared.db import get_supabase
from app.types import ClassSlot

router = APIRouter(dependencies=[Depends(require_internal_secret)])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreateClassEventRequest(BaseModel):
    name: str
    class_schedule: list[ClassSlot]


class CreateStudentFolderRequest(BaseModel):
    name: str
    meet_link: str
    class_schedule: list[ClassSlot]
    mode: str = "My Python Syllabus"


class UpdateClassEventRequest(BaseModel):
    name: str
    class_schedule: list[ClassSlot]
    event_ids: list[str]
    meet_link: str
    drive_folder_url: str | None = None


class DeleteStudentRequest(BaseModel):
    drive_folder_url: str | None = None
    calendar_event_ids: list[str] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _friendly_google_error(raw: str) -> str:
    if "invalid_grant" in raw:
        return "Google auth expired. Visit /api/google/auth to reconnect."
    if "insufficient" in raw.lower() or "403" in raw:
        return "Google API not authorised. Visit /api/google/auth to re-connect."
    return raw


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/auth-url")
async def google_auth_url():
    state = generate_state_token()
    url = build_google_auth_url(state)
    return RedirectResponse(url=url, status_code=302)


@router.get("/callback", response_model=GoogleCallbackResponse)
async def google_callback(
    code: str = Query(...),
    state: str = Query(...),
    supabase=Depends(get_supabase),
):
    try:
        verify_and_consume_state(state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        refresh_token = await exchange_code_for_refresh_token(code)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await supabase.from_("settings").upsert(
        {"key": "google_refresh_token", "value": refresh_token},
        on_conflict="key",
    ).execute()

    return {"ok": True, "message": "Google connected successfully. You can close this tab."}


@router.post("/create-class-event", response_model=CreateClassEventResponse)
async def create_class_event(body: CreateClassEventRequest):
    supabase = await get_supabase()
    try:
        creds, stored_token = await get_oauth2_credentials(supabase)
        result = await create_weekly_class_events(creds, body.name, body.class_schedule)
        await save_token_if_rotated(creds, stored_token, supabase)
        return {
            "meet_link": result["meet_link"],
            "event_count": result["event_count"],
            "event_ids": result["event_ids"],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_friendly_google_error(str(exc))) from exc


@router.post("/create-student-folder", response_model=CreateStudentFolderResponse)
async def create_student_folder(body: CreateStudentFolderRequest):
    resolved_mode = (
        "Other Syllabus" if body.mode == "Other Syllabus" else "My Python Syllabus"
    )
    supabase = await get_supabase()
    try:
        creds, stored_token = await get_oauth2_credentials(supabase)
        folder_url = await create_student_drive_folder(
            creds, body.name, body.meet_link, body.class_schedule, resolved_mode
        )
        await save_token_if_rotated(creds, stored_token, supabase)
        return {"url": folder_url}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_friendly_google_error(str(exc))) from exc


@router.post("/update-class-event", response_model=UpdateClassEventResponse)
async def update_class_event(body: UpdateClassEventRequest):
    supabase = await get_supabase()
    try:
        creds, stored_token = await get_oauth2_credentials(supabase)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_friendly_google_error(str(exc))) from exc

    trimmed_name = body.name.strip()
    trimmed_meet_link = body.meet_link.strip()
    slots = body.class_schedule
    trimmed_drive_url = body.drive_folder_url.strip() if body.drive_folder_url else None

    # Phase 1: find recurring IDs + update Drive doc in parallel
    search_result, drive_result = await asyncio.gather(
        find_recurring_event_ids(creds, trimmed_name),
        update_student_meet_doc(
            creds, trimmed_drive_url, trimmed_name, slots, trimmed_meet_link
        )
        if trimmed_drive_url
        else asyncio.sleep(0),
        return_exceptions=True,
    )

    search_ids: list[str] = search_result if not isinstance(search_result, Exception) else []
    merged_event_ids = list(dict.fromkeys(body.event_ids + search_ids))

    # Phase 2: update Calendar events
    try:
        cal_result = await update_weekly_class_events(
            creds, trimmed_name, slots, merged_event_ids, trimmed_meet_link
        )
    except Exception as exc:
        raw = str(exc)
        raise HTTPException(status_code=500, detail=_friendly_google_error(raw)) from exc

    drive_doc_error: str | None = None
    if isinstance(drive_result, Exception):
        drive_doc_error = str(drive_result)

    new_meet_link: str | None = cal_result.get("meet_link")
    schedule_cleared: bool = cal_result.get("schedule_cleared", False)

    if new_meet_link and trimmed_drive_url:
        try:
            await update_student_meet_doc(
                creds, trimmed_drive_url, trimmed_name, slots, new_meet_link
            )
            drive_doc_error = None
        except Exception as exc:
            drive_doc_error = str(exc)

    await save_token_if_rotated(creds, stored_token, supabase)
    return {
        "event_ids": cal_result["event_ids"],
        "meet_link": new_meet_link,
        "drive_doc_error": drive_doc_error,
        "schedule_cleared": schedule_cleared,
    }


@router.post("/delete-student", response_model=DeleteStudentGoogleResponse)
async def delete_student_google_endpoint(body: DeleteStudentRequest):
    drive_url = body.drive_folder_url
    event_ids = body.calendar_event_ids

    has_drive = bool(drive_url and drive_url.strip())
    has_events = bool(event_ids)

    if not has_drive and not has_events:
        return {"drive_error": None, "calendar_error": None}

    supabase = await get_supabase()
    try:
        creds, stored_token = await get_oauth2_credentials(supabase)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result = await delete_student_google(creds, drive_url, event_ids)
    await save_token_if_rotated(creds, stored_token, supabase)
    return result


@router.post("/sync-all", response_model=SyncAllResponse)
async def sync_all():
    supabase = await get_supabase()
    try:
        creds, stored_token = await get_oauth2_credentials(supabase)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    try:
        results = await sync_all_students(supabase, creds)
        await save_token_if_rotated(creds, stored_token, supabase)
        return {"results": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
