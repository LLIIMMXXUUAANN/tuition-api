"""Timetable settings and slot-generation endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import AsyncClient

from app.auth import require_internal_secret
from app.features.timetable.service import (
    BookedSlot,
    TimetableValidationError,
    run_slot_generation,
    save_buffer_mins,
    save_rules,
)
from app.shared.db import get_supabase
from app.shared.response_models import (
    BufferMinsResponse,
    GenerateSlotsResponse,
    OkResponse,
    RulesResponse,
)
from app.types import ClassSlot

router = APIRouter(dependencies=[Depends(require_internal_secret)], tags=["timetable"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class UpdateRulesRequest(BaseModel):
    rules: str


class UpdateBufferMinsRequest(BaseModel):
    buffer_mins: int


class GenerateSlotsRequest(BaseModel):
    rules: str
    student_availability: str = ""
    booked_slots: list[ClassSlot] = []
    buffer_mins: int = 15


# ---------------------------------------------------------------------------
# Rules endpoints
# ---------------------------------------------------------------------------


@router.get("/rules", response_model=RulesResponse)
async def get_rules(supabase: AsyncClient = Depends(get_supabase)):
    result = (
        await supabase.from_("settings")
        .select("value")
        .eq("key", "timetable_rules")
        .maybe_single()
        .execute()
    )
    value: str = result.data["value"] if (result and result.data) else ""
    return {"rules": value}


@router.post("/rules", response_model=OkResponse)
async def update_rules(body: UpdateRulesRequest, supabase: AsyncClient = Depends(get_supabase)):
    await save_rules(supabase, body.rules)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Buffer mins endpoints
# ---------------------------------------------------------------------------


@router.get("/buffer-mins", response_model=BufferMinsResponse)
async def get_buffer_mins(supabase: AsyncClient = Depends(get_supabase)):
    result = (
        await supabase.from_("settings")
        .select("value")
        .eq("key", "timetable_buffer_mins")
        .maybe_single()
        .execute()
    )
    buffer_mins: int = int(result.data["value"]) if (result and result.data) else 15
    return {"buffer_mins": buffer_mins}


@router.post("/buffer-mins", response_model=OkResponse)
async def update_buffer_mins(body: UpdateBufferMinsRequest, supabase: AsyncClient = Depends(get_supabase)):
    try:
        await save_buffer_mins(supabase, body.buffer_mins)
    except TimetableValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


# ---------------------------------------------------------------------------
# Generate slots endpoint
# ---------------------------------------------------------------------------


@router.post("/generate-slots", response_model=GenerateSlotsResponse)
async def generate_slots(body: GenerateSlotsRequest):
    if not body.rules.strip():
        raise HTTPException(status_code=400, detail="rules is required")
    if body.buffer_mins < 0 or body.buffer_mins > 60:
        raise HTTPException(status_code=400, detail="buffer_mins must be between 0 and 60")

    booked: list[BookedSlot] = [
        BookedSlot(day=s.day, start=s.start, end=s.end) for s in body.booked_slots
    ]

    try:
        slots = await run_slot_generation(
            body.rules.strip(),
            body.student_availability.strip() or None,
            booked,
            body.buffer_mins,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"slots": [{"day": s.day, "time": s.time, "state": s.state} for s in slots]}
