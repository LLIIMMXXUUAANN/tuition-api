"""Payment message builder — port of src/shared/lib/payment.ts."""

from dataclasses import dataclass
from typing import Any

from app.types import ClassSlot
from app.lib.utils import (
    MONTH_NAMES,
    format_fee,
    get_weekday_dates,
    group_slots_by_day,
    ordinal,
    oxford_list,
    time_to_mins,
)


@dataclass(frozen=True)
class PaymentStudentData:
    name: str
    contact_person: str | None
    class_schedule: list[ClassSlot]
    fee_per_hour: float


def build_payment_message(
    student: PaymentStudentData,
    month: int,
    year: int,
    template_type: int,  # 1 or 2
    carryover: float = 0,
) -> dict[str, Any]:
    """Return {"message": str, "month_name": str} or {"error": str}."""

    slots_by_day = group_slots_by_day(student.class_schedule)

    all_dates: list[int] = []
    session_fee_total: float = 0.0

    for day, slots in slots_by_day.items():
        dates = get_weekday_dates(year, month, day)
        all_dates.extend(dates)
        hours_per_session = sum(
            (time_to_mins(s.end) - time_to_mins(s.start)) / 60 for s in slots
        )
        session_fee_total += len(dates) * hours_per_session * student.fee_per_hour

    all_dates.sort()

    if not all_dates:
        return {"error": "No scheduled class days found for this student"}

    session_count = len(all_dates)
    month_name = MONTH_NAMES[month - 1]
    date_list = oxford_list([ordinal(d) for d in all_dates])

    cp = student.contact_person.strip() if student.contact_person else None
    recipient = student.name if (not cp or cp == "-") else cp

    if template_type == 1:
        message = (
            f"Hi {recipient}, just a gentle reminder regarding the tuition fee. "
            f"There are {session_count} sessions in {month_name} ({date_list}), "
            f"bringing the total to RM{format_fee(session_fee_total)}. Thank you 😄"
        )
    else:
        co_fee = carryover * (session_fee_total / session_count)
        co_label = f"{int(carryover)} session{'s' if carryover != 1 else ''}"
        message = (
            f"Hi {recipient}, just a gentle reminder regarding the tuition fee. "
            f"There are {session_count} sessions in {month_name} ({date_list}). "
            f"With {co_label} carried over from the previous classes, "
            f"bringing the total to RM{format_fee(session_fee_total - co_fee)}. Thank you. 😄"
        )

    return {"message": message, "month_name": month_name}
