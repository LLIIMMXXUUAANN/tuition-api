"""Template tool implementations — port of src/features/agent/lib/tools/template-tools.ts."""

from __future__ import annotations

from supabase import AsyncClient

from app.shared.errors import err_msg
from app.features.payment.service import PaymentStudentData, PaymentValidationError, build_payment_message
from app.features.templates.service import TEMPLATE_META, template_meta
from app.shared.utils import get_myt_now
from app.types import ClassSlot


def list_templates() -> dict:
    """Pure function — no DB call. Returns {templates: [{id, title, description}]}."""
    return {
        "templates": [{"id": k, **v} for k, v in TEMPLATE_META.items()]
    }


async def get_template(supabase: AsyncClient, id: str) -> dict:
    try:
        result = (
            await supabase.from_("templates")
            .select("id, content")
            .eq("id", id)
            .maybe_single()
            .execute()
        )
    except Exception as exc:
        return {"error": err_msg(exc)}
    if not result.data:
        return {"error": f'Template "{id}" not found'}

    meta = template_meta(result.data["id"])
    return {
        "template": {
            "id": result.data["id"],
            "title": meta["title"],
            "description": meta["description"],
            "content": result.data["content"],
        }
    }


async def generate_payment_message(supabase: AsyncClient, params: dict) -> dict:
    now_myt = get_myt_now()

    # Default to next calendar month
    if now_myt.month == 12:
        next_month, next_year = 1, now_myt.year + 1
    else:
        next_month, next_year = now_myt.month + 1, now_myt.year

    resolved_month = params.get("month") or next_month
    resolved_year = params.get("year") or next_year
    template_type = params.get("template_type") or 1
    carryover = params.get("carryover") or 0.0

    student_id = params["student_id"]
    try:
        fetch_result = (
            await supabase.from_("students")
            .select("name, contact_person, class_schedule, fee_per_hour, status")
            .eq("id", student_id)
            .single()
            .execute()
        )
    except Exception as exc:
        return {"error": err_msg(exc)}

    if not fetch_result.data:
        return {"error": "Student not found"}

    student_data = fetch_result.data
    schedule = [ClassSlot(**s) for s in (student_data.get("class_schedule") or [])]
    student = PaymentStudentData(
        name=student_data["name"],
        contact_person=student_data.get("contact_person"),
        class_schedule=schedule,
        fee_per_hour=student_data["fee_per_hour"],
    )

    try:
        result = build_payment_message(
            student=student,
            month=resolved_month,
            year=resolved_year,
            template_type=template_type,
            carryover=carryover,
        )
    except PaymentValidationError as exc:
        return {"error": str(exc)}

    return {
        "message": result["message"],
        "month": resolved_month,
        "year": resolved_year,
        "month_name": result["month_name"],
    }
