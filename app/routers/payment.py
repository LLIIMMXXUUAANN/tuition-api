"""Payment message generation endpoint."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import require_internal_secret
from app.lib.payment import PaymentStudentData, build_payment_message
from app.services.supabase_client import get_supabase
from app.types import ClassSlot

router = APIRouter(dependencies=[Depends(require_internal_secret)])


class GeneratePaymentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # Accept camelCase keys from the TypeScript client
    student_id: str = Field(alias="studentId")
    month: int
    year: int
    template_type: int = Field(alias="templateType")
    carryover: float = Field(default=0.0, alias="carryover")


@router.post("/generate")
async def generate_payment(body: GeneratePaymentRequest):
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
    supabase = await get_supabase()
    result = (
        await supabase.from_("students")
        .select("name, contact_person, class_schedule, fee_per_hour, status")
        .eq("id", body.student_id)
        .maybe_single()
        .execute()
    )

    student_data = result.data if result is not None else None
    if not student_data:
        raise HTTPException(status_code=404, detail="Student not found")

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

    outcome = build_payment_message(
        student=student,
        month=body.month,
        year=body.year,
        template_type=body.template_type,
        carryover=body.carryover,
    )

    if "error" in outcome:
        raise HTTPException(status_code=400, detail=outcome["error"])

    return {"message": outcome["message"]}
