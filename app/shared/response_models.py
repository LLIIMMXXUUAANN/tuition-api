"""Pydantic response models for all API endpoints."""
from __future__ import annotations

from pydantic import BaseModel

from app.types import ClassSlot


class OkResponse(BaseModel):
    ok: bool = True


# ---------------------------------------------------------------------------
# Student
# ---------------------------------------------------------------------------


class StudentResponse(BaseModel):
    id: str
    name: str
    mode: str
    fee_per_hour: float
    payment_method: str | None = None
    status: str
    class_schedule: list[ClassSlot] | None = None
    contact_person: str | None = None
    contact_phone: str | None = None
    student_phone: str | None = None
    google_meet_link: str | None = None
    google_drive_link: str | None = None
    calendar_event_ids: list[str] | None = None
    access_emails: list[str] | None = None
    today_homework: str | None = None
    notes: str | None = None
    latest_payment: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class CreateStudentResponse(BaseModel):
    id: str
    google_warning: str | None = None


class MutateStudentResponse(BaseModel):
    ok: bool = True
    google_warning: str | None = None


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


class TemplateItem(BaseModel):
    id: str
    content: str


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------


class PaymentResponse(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Timetable
# ---------------------------------------------------------------------------


class RulesResponse(BaseModel):
    rules: str


class BufferMinsResponse(BaseModel):
    buffer_mins: int


class SlotItem(BaseModel):
    day: str
    time: str
    state: str


class GenerateSlotsResponse(BaseModel):
    slots: list[SlotItem]


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------


class GoogleCallbackResponse(BaseModel):
    ok: bool = True
    message: str


class CreateClassEventResponse(BaseModel):
    meet_link: str
    event_count: int
    event_ids: list[str]


class CreateStudentFolderResponse(BaseModel):
    url: str


class UpdateClassEventResponse(BaseModel):
    event_ids: list[str]
    meet_link: str | None = None
    drive_doc_error: str | None = None
    schedule_cleared: bool = False


class DeleteStudentGoogleResponse(BaseModel):
    drive_error: str | None = None
    calendar_error: str | None = None


class SyncResultItem(BaseModel):
    name: str
    status: str
    reason: str | None = None


class SyncAllResponse(BaseModel):
    results: list[SyncResultItem]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    steps: list[str] = []
    is_error: bool = False
    students: list[dict] | None = None
    schedule_students: list[dict] | None = None
    slot_data: list[dict] | None = None
    timestamp: str


class ConversationResponse(BaseModel):
    id: str
    messages: list[MessageResponse]


class MessagesResponse(BaseModel):
    messages: list[MessageResponse]
