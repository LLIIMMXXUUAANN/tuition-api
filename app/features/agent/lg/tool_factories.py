"""LangGraph tool factories — wrap the 18 shared tool implementations in StructuredTool instances.

Port of src/features/agent/lib/lg/tool-factories.ts.
"""

from __future__ import annotations

from typing import Literal, Optional

from app.types import PaymentMethod, StudentMode, StudentStatus, WeekDay

from langchain_core.tools import StructuredTool
from langgraph.config import get_stream_writer
from pydantic import BaseModel, Field

from app.features.agent.tools.student_tools import (
    ALLOWED_UPDATE_KEYS,
    create_student,
    delete_student,
    get_fee_summary,
    get_schedule,
    get_student,
    list_students,
    manage_portal_access,
    run_sync_all,
    search_students,
    update_student,
)
from app.features.agent.tools.template_tools import (
    generate_payment_message,
    get_template,
    list_templates,
)
from app.features.agent.tools.timetable_tools import (
    download_timetable_image,
    generate_slot_availability,
    get_timetable_settings,
    update_buffer_mins,
    update_timetable_rules,
)


# ---------------------------------------------------------------------------
# Shared schema types
# ---------------------------------------------------------------------------


class ClassSlotInput(BaseModel):
    day: WeekDay
    start: str = Field(description='24-hour HH:MM format, e.g. "15:00"')
    end: str = Field(description='24-hour HH:MM format, e.g. "17:00"')


class NoArgInput(BaseModel):
    reason: Optional[str] = Field(default=None, description="Optional reason for invoking this tool")


class CannotCompleteInput(BaseModel):
    reason: str = Field(description="Why the task cannot be completed with available tools")


def make_cannot_complete_tool() -> StructuredTool:
    """Return a cannot_complete tool — a structured signal for when a subagent lacks suitable tools."""

    def cannot_complete(reason: str) -> str:
        return f"Cannot complete: {reason}"

    return StructuredTool.from_function(
        func=cannot_complete,
        name="cannot_complete",
        description="Call this when the assigned task cannot be completed with your available tools. State a clear reason.",
        args_schema=CannotCompleteInput,
    )


# ---------------------------------------------------------------------------
# Student tool schemas
# ---------------------------------------------------------------------------


class SearchStudentsInput(BaseModel):
    query: str = Field(description="Partial or full student name to search for")


class GetStudentInput(BaseModel):
    id: str = Field(description="Student UUID")


class ListStudentsInput(BaseModel):
    status: Optional[StudentStatus] = Field(
        default=None,
        description="Filter by student status. Omit for all students.",
    )


class StudentFieldsInput(BaseModel):
    id: str = Field(description="Student UUID to update")
    fields: dict = Field(
        description=(
            f"Fields to update. Allowed keys: {', '.join(sorted(ALLOWED_UPDATE_KEYS))}. "
            "For class_schedule, provide a list of {day, start, end} objects."
        )
    )


class DeleteStudentInput(BaseModel):
    id: str = Field(description="Student UUID to permanently delete")


class ManagePortalAccessInput(BaseModel):
    student_id: str = Field(description="Student UUID")
    action: Literal["add", "remove"]
    email: str = Field(description="Email address to add or remove from portal access")


class GetScheduleInput(BaseModel):
    day: WeekDay


class GetFeeSummaryInput(BaseModel):
    month: Optional[int] = Field(default=None, description="Month number 1–12. Omit for current month.")
    year: Optional[int] = Field(default=None, description="4-digit year. Omit for current year.")


class CreateStudentInput(BaseModel):
    name: str = Field(description="Student's full name")
    mode: StudentMode
    fee_per_hour: float = Field(description="Hourly fee in RM")
    payment_method: Optional[PaymentMethod] = Field(default=None, description="Defaults to 'Monthly'.")
    status: Optional[StudentStatus] = Field(default=None, description="Defaults to 'Active'.")
    class_schedule: Optional[list[ClassSlotInput]] = Field(default=None, description="Weekly class slots")
    contact_person: Optional[str] = Field(default=None, description="Parent/guardian name")
    contact_phone: Optional[str] = Field(default=None, description="Parent/guardian phone")
    student_phone: Optional[str] = Field(default=None, description="Student's own phone")
    today_homework: Optional[str] = Field(default=None, description="Current homework assignment")
    notes: Optional[str] = Field(default=None, description="Free-form notes")
    latest_payment: Optional[str] = Field(default=None, description="Latest payment date/info")
    google_meet_link: Optional[str] = Field(default=None, description="Google Meet link")
    google_drive_link: Optional[str] = Field(default=None, description="Google Drive folder link")
    access_emails: Optional[list[str]] = Field(default=None, description="Portal access emails")


# ---------------------------------------------------------------------------
# Template tool schemas
# ---------------------------------------------------------------------------


class GetTemplateInput(BaseModel):
    id: str = Field(
        description="Template ID: 'payment', 'review_request1', 'review_request2', 'recommendation', 'first_approach'"
    )


class GeneratePaymentMessageInput(BaseModel):
    student_id: str = Field(description="Student UUID")
    month: Optional[int] = Field(default=None, description="Month number 1–12. Omit for next month.")
    year: Optional[int] = Field(default=None, description="4-digit year. Omit for next year.")
    template_type: Optional[int] = Field(default=None, description="1 or 2. Defaults to 1.")
    carryover: Optional[float] = Field(default=None, description="Carryover amount in RM. Required for template_type 2.")


# ---------------------------------------------------------------------------
# Timetable tool schemas
# ---------------------------------------------------------------------------


class UpdateTimetableRulesInput(BaseModel):
    rules: str = Field(description="The new scheduling rules text to save")


class UpdateBufferMinsInput(BaseModel):
    buffer_mins: int = Field(ge=0, le=60, description="Buffer minutes around booked classes")


class GenerateSlotAvailabilityInput(BaseModel):
    student_availability: Optional[str] = Field(
        default=None,
        description="Optional description of a prospective student's availability constraints"
    )


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def make_student_tools(supabase) -> list[StructuredTool]:
    """Return all 10 student tools bound to the given Supabase client."""

    async def _search_students(query: str) -> dict:
        return await search_students(supabase, query)

    async def _get_student(id: str) -> dict:
        return await get_student(supabase, id)

    async def _list_students(status: Optional[str] = None) -> dict:
        return await list_students(supabase, {"status": status} if status else {})

    async def _create_student(
        name: str,
        mode: str,
        fee_per_hour: float,
        payment_method: Optional[str] = None,
        status: Optional[str] = None,
        class_schedule: Optional[list[ClassSlotInput]] = None,
        contact_person: Optional[str] = None,
        contact_phone: Optional[str] = None,
        student_phone: Optional[str] = None,
        today_homework: Optional[str] = None,
        notes: Optional[str] = None,
        latest_payment: Optional[str] = None,
        google_meet_link: Optional[str] = None,
        google_drive_link: Optional[str] = None,
        access_emails: Optional[list[str]] = None,
    ) -> dict:
        params = {
            "name": name,
            "mode": mode,
            "fee_per_hour": fee_per_hour,
            "payment_method": payment_method,
            "status": status,
            "class_schedule": [s.model_dump() for s in class_schedule] if class_schedule else None,
            "contact_person": contact_person,
            "contact_phone": contact_phone,
            "student_phone": student_phone,
            "today_homework": today_homework,
            "notes": notes,
            "latest_payment": latest_payment,
            "google_meet_link": google_meet_link,
            "google_drive_link": google_drive_link,
            "access_emails": access_emails,
        }
        return await create_student(supabase, params)

    async def _update_student(id: str, fields: dict) -> dict:
        # Deserialize class_schedule if present
        if "class_schedule" in fields and isinstance(fields["class_schedule"], list):
            fields = dict(fields)
            fields["class_schedule"] = [
                s if isinstance(s, dict) else s.model_dump()
                for s in fields["class_schedule"]
            ]
        return await update_student(supabase, id, fields)

    async def _delete_student(id: str) -> dict:
        return await delete_student(supabase, id)

    async def _sync_all_students(reason: Optional[str] = None) -> dict:
        return await run_sync_all(supabase)

    async def _manage_portal_access(student_id: str, action: str, email: str) -> dict:
        return await manage_portal_access(supabase, student_id, action, email)

    async def _get_schedule(day: str) -> dict:
        return await get_schedule(supabase, day)

    async def _get_fee_summary(month: Optional[int] = None, year: Optional[int] = None) -> dict:
        return await get_fee_summary(supabase, month, year)

    return [
        StructuredTool.from_function(
            coroutine=_search_students,
            name="search_students",
            description="Search for students by name (partial match). Returns id, name, status, class_schedule.",
            args_schema=SearchStudentsInput,
        ),
        StructuredTool.from_function(
            coroutine=_get_student,
            name="get_student",
            description="Get full details for a student by UUID.",
            args_schema=GetStudentInput,
        ),
        StructuredTool.from_function(
            coroutine=_list_students,
            name="list_students",
            description="List all students with their id, name, status, mode, fee_per_hour, class_schedule. Optionally filter by status.",
            args_schema=ListStudentsInput,
        ),
        StructuredTool.from_function(
            coroutine=_create_student,
            name="create_student",
            description="Create a new student record. Requires name, mode, and fee_per_hour.",
            args_schema=CreateStudentInput,
        ),
        StructuredTool.from_function(
            coroutine=_update_student,
            name="update_student",
            description="Update one or more fields on a student record by UUID.",
            args_schema=StudentFieldsInput,
        ),
        StructuredTool.from_function(
            coroutine=_delete_student,
            name="delete_student",
            description="Permanently delete a student record and clean up Google Calendar/Drive. Requires explicit confirmation.",
            args_schema=DeleteStudentInput,
        ),
        StructuredTool.from_function(
            coroutine=_sync_all_students,
            name="sync_all_students",
            description="Sync all active students' Google Calendar events and Drive Meet docs to match the DB schedule. Requires explicit confirmation.",
            args_schema=NoArgInput,
        ),
        StructuredTool.from_function(
            coroutine=_manage_portal_access,
            name="manage_portal_access",
            description="Add or remove a portal access email for a student.",
            args_schema=ManagePortalAccessInput,
        ),
        StructuredTool.from_function(
            coroutine=_get_schedule,
            name="get_schedule",
            description="Get all active students with classes on a given day of the week.",
            args_schema=GetScheduleInput,
        ),
        StructuredTool.from_function(
            coroutine=_get_fee_summary,
            name="get_fee_summary",
            description="Calculate monthly fees for all active students. Omit month/year to use current month.",
            args_schema=GetFeeSummaryInput,
        ),
        make_cannot_complete_tool(),
    ]


def make_template_tools(supabase) -> list[StructuredTool]:
    """Return all 3 template tools bound to the given Supabase client."""

    def _list_templates(reason: Optional[str] = None) -> dict:
        return list_templates()

    async def _get_template(id: str) -> dict:
        return await get_template(supabase, id)

    async def _generate_payment_message(
        student_id: str,
        month: Optional[int] = None,
        year: Optional[int] = None,
        template_type: Optional[int] = None,
        carryover: Optional[float] = None,
    ) -> dict:
        params = {
            "student_id": student_id,
            "month": month,
            "year": year,
            "template_type": template_type,
            "carryover": carryover,
        }
        return await generate_payment_message(supabase, params)

    return [
        StructuredTool.from_function(
            func=_list_templates,
            name="list_templates",
            description="List all available message templates (id, title, description). No DB call needed.",
            args_schema=NoArgInput,
        ),
        StructuredTool.from_function(
            coroutine=_get_template,
            name="get_template",
            description="Get the full content of a specific message template by id.",
            args_schema=GetTemplateInput,
        ),
        StructuredTool.from_function(
            coroutine=_generate_payment_message,
            name="generate_payment_message",
            description=(
                "Generate a personalised payment reminder message for a student. "
                "Requires student_id (UUID). Omit month/year to default to next month."
            ),
            args_schema=GeneratePaymentMessageInput,
        ),
        make_cannot_complete_tool(),
    ]


def make_timetable_tools(supabase) -> list[StructuredTool]:
    """Return all 5 timetable tools bound to the given Supabase client."""

    async def _get_timetable_settings(reason: Optional[str] = None) -> dict:
        return await get_timetable_settings(supabase)

    async def _update_timetable_rules(rules: str) -> dict:
        return await update_timetable_rules(supabase, rules)

    async def _update_buffer_mins(buffer_mins: int) -> dict:
        return await update_buffer_mins(supabase, buffer_mins)

    async def _generate_slot_availability(student_availability: Optional[str] = None) -> dict:
        result = await generate_slot_availability(supabase, student_availability or "")
        try:
            writer = get_stream_writer()
            if writer and "slots" in result:
                writer({"slots_ready": result["slots"]})
        except Exception:
            pass
        return result

    async def _download_timetable_image(reason: Optional[str] = None) -> dict:
        result = await download_timetable_image(supabase)
        try:
            writer = get_stream_writer()
            if writer and "students" in result:
                writer({"download_schedule": result["students"]})
        except Exception:
            pass
        return result

    return [
        StructuredTool.from_function(
            coroutine=_get_timetable_settings,
            name="get_timetable_settings",
            description="Read current timetable scheduling rules and buffer minutes from the database.",
            args_schema=NoArgInput,
        ),
        StructuredTool.from_function(
            coroutine=_update_timetable_rules,
            name="update_timetable_rules",
            description="Save new timetable scheduling rules text to the database.",
            args_schema=UpdateTimetableRulesInput,
        ),
        StructuredTool.from_function(
            coroutine=_update_buffer_mins,
            name="update_buffer_mins",
            description="Save the buffer minutes setting (0–60) to the database.",
            args_schema=UpdateBufferMinsInput,
        ),
        StructuredTool.from_function(
            coroutine=_generate_slot_availability,
            name="generate_slot_availability",
            description=(
                "Generate AI-classified slot availability (preferred/normal/unavailable) "
                "based on timetable rules, buffer mins, and booked classes. "
                "Emits a download button in the chat UI."
            ),
            args_schema=GenerateSlotAvailabilityInput,
        ),
        StructuredTool.from_function(
            coroutine=_download_timetable_image,
            name="download_timetable_image",
            description=(
                "Fetch all active students' schedules and emit a download button for "
                "the weekly schedule PNG image."
            ),
            args_schema=NoArgInput,
        ),
        make_cannot_complete_tool(),
    ]
