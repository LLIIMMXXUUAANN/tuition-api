"""Payment message generation endpoint."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import AsyncClient

from app.auth import require_internal_secret
from app.features.payment.service import PaymentStudentData, PaymentValidationError, build_payment_message
from app.shared.db import get_supabase
from app.shared.response_models import PaymentResponse
from app.types import ClassSlot

router = APIRouter(dependencies=[Depends(require_internal_secret)], tags=["payment"])


class GeneratePaymentRequest(BaseModel):
    student_id: str
    month: int
    year: int
    template_type: int
    carryover: float = 0.0


@router.post("/generate", response_model=PaymentResponse)
async def generate_payment(body: GeneratePaymentRequest, supabase: AsyncClient = Depends(get_supabase)):
    # Validate ranges (mirror TypeScript exactly)
    if body.month < 1 or body.month > 12 or body.year < 2020 or body.year > 2100:
        raise HTTPException(status_code=400, detail="month or year out of range")

    if body.template_type not in (1, 2):
        raise HTTPException(status_code=400, detail="templateType must be 1 or 2")

    if body.template_type == 2 and body.carryover <= 0:
        raise HTTPException(
            status_code=400, detail="carryover is required for templateType 2"
        )

    # Fetch student from Supabase
    try:
        result = (
            await supabase.from_("students")
            .select("name, contact_person, class_schedule, fee_per_hour, status")
            .eq("id", body.student_id)
            .maybe_single()
            .execute()
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not result.data:
        raise HTTPException(status_code=404, detail="Student not found")

    student_data = result.data
    if student_data["status"] != "Active":
        raise HTTPException(status_code=400, detail="Student is not active")

    # Build student object
    schedule = [ClassSlot(**s) for s in (student_data.get("class_schedule") or [])]
    student = PaymentStudentData(
        name=student_data["name"],
        contact_person=student_data.get("contact_person"),
        class_schedule=schedule,
        fee_per_hour=float(student_data["fee_per_hour"]),
    )

    try:
        outcome = build_payment_message(
            student=student,
            month=body.month,
            year=body.year,
            template_type=body.template_type,
            carryover=body.carryover,
        )
    except PaymentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"message": outcome["message"]}
