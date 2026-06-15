"""Template tool implementations — port of src/features/agent/lib/tools/template-tools.ts."""

from __future__ import annotations

import datetime

import pytz
from supabase import AsyncClient

from app.features.payment.service import PaymentStudentData, PaymentValidationError, build_payment_message
from app.features.templates.service import TEMPLATE_META, template_meta
from app.types import ClassSlot


def list_templates() -> dict:
    """Pure function — no DB call. Returns {templates: [{id, title, description}]}."""
    return {
        "templates": [{"id": k, **v} for k, v in TEMPLATE_META.items()]
    }


async def get_template(supabase: AsyncClient, id: str) -> dict:
    result = (
        await supabase.from_("templates")
        .select("id, content")
        .eq("id", id)
        .maybe_single()
        .execute()
    )
    if result is None or not result.data:
        return {"error": f'Template "{id}" not found'}
    if hasattr(result, "error") and result.error:
        return {"error": result.error.message}

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
    MYT = pytz.timezone("Asia/Kuala_Lumpur")
    now_myt = datetime.datetime.now(tz=MYT)

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
    fetch_result = (
        await supabase.from_("students")
        .select("name, contact_person, class_schedule, fee_per_hour, status")
        .eq("id", student_id)
        .single()
        .execute()
    )

    if (hasattr(fetch_result, "error") and fetch_result.error) or not fetch_result.data:
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
        "monthName": result["month_name"],
    }
