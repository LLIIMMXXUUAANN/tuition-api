from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

StudentMode = Literal["My Python Syllabus", "Other Syllabus"]
PaymentMethod = Literal["Monthly", "Weekly"]
StudentStatus = Literal["Active", "On Hold", "Completed"]
SlotState = Literal["preferred", "normal", "unavailable"]


class WeekDay(StrEnum):
    Monday = "Monday"
    Tuesday = "Tuesday"
    Wednesday = "Wednesday"
    Thursday = "Thursday"
    Friday = "Friday"
    Saturday = "Saturday"
    Sunday = "Sunday"


class ClassSlot(BaseModel):
    day: WeekDay
    start: str  # "HH:MM"
    end: str    # "HH:MM"


class Student(BaseModel):
    id: str
    name: str
    mode: StudentMode
    fee_per_hour: float
    payment_method: PaymentMethod | None = None
    status: StudentStatus
    class_schedule: list[ClassSlot] = []
    contact_person: str | None = None
    contact_phone: str | None = None
    student_phone: str | None = None
    google_meet_link: str | None = None
    google_drive_link: str | None = None
    calendar_event_ids: list[str] = []
    access_emails: list[str] = []
    today_homework: str | None = None
    notes: str | None = None
    latest_payment: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
