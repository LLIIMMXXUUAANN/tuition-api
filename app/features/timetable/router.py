"""Timetable settings and slot-generation endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import require_internal_secret
from app.features.timetable.service import BookedSlot, run_slot_generation
from app.shared.db import get_supabase
from app.shared.schema import CamelResponse
from app.types import ClassSlot

router = APIRouter(dependencies=[Depends(require_internal_secret)], default_response_class=CamelResponse)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class UpdateRulesRequest(BaseModel):
    rules: str


class UpdateBufferMinsRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # Accept both camelCase (TypeScript client) and snake_case
    buffer_mins: int = Field(alias="bufferMins")


class GenerateSlotsRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    rules: str
    student_availability: str = Field(default="", alias="studentAvailability")
    booked_slots: list[ClassSlot] = Field(default_factory=list, alias="bookedSlots")
    buffer_mins: int = Field(default=15, alias="bufferMins")


# ---------------------------------------------------------------------------
# Rules endpoints
# ---------------------------------------------------------------------------


@router.get("/rules")
async def get_rules():
    supabase = await get_supabase()
    result = (
        await supabase.from_("settings")
        .select("value")
        .eq("key", "timetable_rules")
        .maybe_single()
        .execute()
    )
    value: str = result.data["value"] if (result and result.data) else ""
    return {"rules": value}


@router.post("/rules")
async def update_rules(body: UpdateRulesRequest):
    if not isinstance(body.rules, str):
        raise HTTPException(status_code=400, detail="rules must be a string")

    supabase = await get_supabase()
    await supabase.from_("settings").upsert(
        {"key": "timetable_rules", "value": body.rules}, on_conflict="key"
    ).execute()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Buffer mins endpoints
# ---------------------------------------------------------------------------


@router.get("/buffer-mins")
async def get_buffer_mins():
    supabase = await get_supabase()
    result = (
        await supabase.from_("settings")
        .select("value")
        .eq("key", "timetable_buffer_mins")
        .maybe_single()
        .execute()
    )
    buffer_mins: int = int(result.data["value"]) if (result and result.data) else 15
    return {"buffer_mins": buffer_mins}


@router.post("/buffer-mins")
async def update_buffer_mins(body: UpdateBufferMinsRequest):
    if body.buffer_mins < 0 or body.buffer_mins > 60:
        raise HTTPException(
            status_code=400, detail="buffer_mins must be between 0 and 60"
        )

    supabase = await get_supabase()
    await supabase.from_("settings").upsert(
        {"key": "timetable_buffer_mins", "value": str(body.buffer_mins)},
        on_conflict="key",
    ).execute()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Generate slots endpoint
# ---------------------------------------------------------------------------


@router.post("/generate-slots")
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
