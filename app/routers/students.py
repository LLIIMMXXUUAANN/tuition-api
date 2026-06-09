"""Student + template CRUD endpoints — handles writes that were previously in browser Supabase client."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import require_internal_secret
from app.services.supabase_client import get_supabase

router = APIRouter(dependencies=[Depends(require_internal_secret)])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ClassSlotInput(BaseModel):
    day: str
    start: str
    end: str


class StudentPayload(BaseModel):
    name: str
    mode: str
    fee_per_hour: float
    payment_method: str = "Monthly"
    status: str = "Active"
    class_schedule: list[ClassSlotInput] | None = None
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
    class_schedule: list[ClassSlotInput] | None = None
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


class TemplateUpdatePayload(BaseModel):
    content: str


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
    data: dict[str, Any] = {}
    for field, value in body.model_dump(exclude_unset=True).items():
        if field == "class_schedule" and value is not None:
            data[field] = [s if isinstance(s, dict) else s for s in value]
        else:
            data[field] = value

    if not data:
        raise HTTPException(status_code=400, detail="No fields provided")

    result = await supabase.from_("students").update(data).eq("id", student_id).execute()
    if hasattr(result, "error") and result.error:
        raise HTTPException(status_code=400, detail=result.error.message)
    return {"ok": True}


@router.delete("/students/{student_id}")
async def delete_student(student_id: str):
    supabase = await get_supabase()
    result = await supabase.from_("students").delete().eq("id", student_id).execute()
    if hasattr(result, "error") and result.error:
        raise HTTPException(status_code=400, detail=result.error.message)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Template endpoint
# ---------------------------------------------------------------------------


@router.put("/templates/{template_id}")
async def update_template(template_id: str, body: TemplateUpdatePayload):
    supabase = await get_supabase()
    result = (
        await supabase.from_("templates")
        .upsert({"id": template_id, "content": body.content}, on_conflict="id")
        .execute()
    )
    if hasattr(result, "error") and result.error:
        raise HTTPException(status_code=400, detail=result.error.message)
    return {"ok": True}
