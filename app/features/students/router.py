"""Student CRUD endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from supabase import AsyncClient

from app.auth import require_internal_secret
from app.features.students.service import (
    IdempotencyKeyConflictError,
    IdempotencyPayloadMismatchError,
    StudentNotFoundError,
)
from app.features.students.service import create_student as svc_create
from app.features.students.service import delete_student as svc_delete
from app.features.students.service import update_student as svc_update
from app.shared.db import get_supabase
from app.shared.response_models import (
    CreateStudentResponse,
    MutateStudentResponse,
    StudentResponse,
)
from app.types import ClassSlot

router = APIRouter(dependencies=[Depends(require_internal_secret)], tags=["students"])


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


@router.get("", response_model=list[StudentResponse])
async def list_students(status: str | None = None, supabase: AsyncClient = Depends(get_supabase)):
    query = supabase.from_("students").select("*").order("name")
    if status:
        query = query.eq("status", status)
    try:
        result = await query.execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result.data or []


@router.get("/portal-lookup", response_model=StudentResponse)
async def portal_lookup(email: str, supabase: AsyncClient = Depends(get_supabase)):
    try:
        result = (
            await supabase.from_("students")
            .select("*")
            .contains("access_emails", [email])
            .limit(1)
            .maybe_single()
            .execute()
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not result.data:
        raise HTTPException(status_code=404, detail="Student not found")
    return result.data


@router.get("/{student_id}", response_model=StudentResponse)
async def get_student(student_id: str, supabase: AsyncClient = Depends(get_supabase)):
    try:
        result = (
            await supabase.from_("students")
            .select("*")
            .eq("id", student_id)
            .maybe_single()
            .execute()
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not result.data:
        raise HTTPException(status_code=404, detail="Student not found")
    return result.data


@router.post("", status_code=201, response_model=CreateStudentResponse)
async def create_student(
    body: StudentPayload, request: Request, supabase: AsyncClient = Depends(get_supabase)
):
    idempotency_key = request.headers.get("Idempotency-Key")
    try:
        result = await svc_create(supabase, body.model_dump(), idempotency_key=idempotency_key)
    except IdempotencyKeyConflictError:
        raise HTTPException(
            status_code=409, detail="A request with this Idempotency-Key is already in progress"
        )
    except IdempotencyPayloadMismatchError:
        raise HTTPException(
            status_code=422, detail="This Idempotency-Key was already used with a different request"
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"id": result["id"], "google_warning": result.get("google_warning")}


@router.put("/{student_id}", response_model=MutateStudentResponse)
async def update_student(student_id: str, body: StudentUpdatePayload, supabase: AsyncClient = Depends(get_supabase)):
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


@router.delete("/{student_id}", response_model=MutateStudentResponse)
async def delete_student(student_id: str, supabase: AsyncClient = Depends(get_supabase)):
    try:
        result = await svc_delete(supabase, student_id)
    except StudentNotFoundError:
        raise HTTPException(status_code=404, detail="Student not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "google_warning": result.get("google_warning")}
