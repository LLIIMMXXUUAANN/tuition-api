"""Student CRUD endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import require_internal_secret
from app.shared.schema import CamelResponse
from app.shared.db import get_supabase
from app.types import ClassSlot
from app.features.students.service import (
    StudentNotFoundError,
    create_student as svc_create,
    update_student as svc_update,
    delete_student as svc_delete,
)

router = APIRouter(dependencies=[Depends(require_internal_secret)], default_response_class=CamelResponse)


# ---------------------------------------------------------------------------
# Request models
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
# Student endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_students(status: str | None = None):
    supabase = await get_supabase()
    query = supabase.from_("students").select("*").order("name")
    if status:
        query = query.eq("status", status)
    result = await query.execute()
    return JSONResponse(content=result.data or [])


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
    return JSONResponse(content=result.data)


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
    return JSONResponse(content=result.data)


@router.post("", status_code=201)
async def create_student(body: StudentPayload):
    supabase = await get_supabase()
    try:
        result = await svc_create(supabase, body.model_dump())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"id": result["id"], "google_warning": result.get("google_warning")}


@router.put("/{student_id}")
async def update_student(student_id: str, body: StudentUpdatePayload):
    supabase = await get_supabase()
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields provided")
    try:
        result = await svc_update(supabase, student_id, fields)
    except StudentNotFoundError:
        raise HTTPException(status_code=404, detail="Student not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "google_warning": result.get("google_warning")}


@router.delete("/{student_id}")
async def delete_student(student_id: str):
    supabase = await get_supabase()
    try:
        result = await svc_delete(supabase, student_id)
    except StudentNotFoundError:
        raise HTTPException(status_code=404, detail="Student not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "google_warning": result.get("google_warning")}
