"""Student CRUD endpoints — handles writes that were previously in browser Supabase client."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import require_internal_secret
from app.services.google.auth import get_oauth2_credentials
from app.services.google.cleanup import delete_student_google
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
    google_meet_link: str | None = None
    google_drive_link: str | None = None
    calendar_event_ids: list[str] | None = None
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
    google_meet_link: str | None = None
    google_drive_link: str | None = None
    calendar_event_ids: list[str] | None = None
    access_emails: list[str] | None = None


# ---------------------------------------------------------------------------
# Student endpoints
# ---------------------------------------------------------------------------


@router.post("/students", status_code=201)
async def create_student(body: StudentPayload):
    supabase = await get_supabase()
    data = body.model_dump()
    if data.get("class_schedule"):
        data["class_schedule"] = [s if isinstance(s, dict) else s.model_dump() for s in (body.class_schedule or [])]
    else:
        data["class_schedule"] = []

    result = await supabase.from_("students").insert(data).execute()
    if hasattr(result, "error") and result.error:
        raise HTTPException(status_code=400, detail=result.error.message)
    if not result.data:
        raise HTTPException(status_code=500, detail="Insert returned no data")
    return {"id": result.data[0]["id"]}


@router.put("/students/{student_id}")
async def update_student(student_id: str, body: StudentUpdatePayload):
    supabase = await get_supabase()
    # Only include fields explicitly provided (exclude_unset strips unset fields)
    data = body.model_dump(exclude_unset=True)

    if not data:
        raise HTTPException(status_code=400, detail="No fields provided")

    result = await supabase.from_("students").update(data).eq("id", student_id).execute()
    if hasattr(result, "error") and result.error:
        raise HTTPException(status_code=400, detail=result.error.message)
    return {"ok": True}


@router.delete("/students/{student_id}")
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
            creds = await get_oauth2_credentials(supabase)
            google_errors = await delete_student_google(creds, drive_url, event_ids)
        except Exception as exc:
            google_errors = {"google_error": str(exc)}

    result = await supabase.from_("students").delete().eq("id", student_id).execute()
    if hasattr(result, "error") and result.error:
        raise HTTPException(status_code=400, detail=result.error.message)
    return {"ok": True, **google_errors}