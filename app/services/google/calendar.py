import asyncio
import time
from datetime import datetime, timedelta
from uuid import uuid4

import pytz
from google.oauth2.credentials import Credentials

from app.config import settings
from app.services.google.auth import build_calendar
from app.types import ClassSlot

BYDAY: dict[str, str] = {
    "Sunday": "SU",
    "Monday": "MO",
    "Tuesday": "TU",
    "Wednesday": "WE",
    "Thursday": "TH",
    "Friday": "FR",
    "Saturday": "SA",
}

MYT = pytz.timezone("Asia/Kuala_Lumpur")

# JS getDay(): Sun=0, Mon=1, ..., Sat=6
JS_DAY_INDEX: dict[str, int] = {
    "Sunday": 0,
    "Monday": 1,
    "Tuesday": 2,
    "Wednesday": 3,
    "Thursday": 4,
    "Friday": 5,
    "Saturday": 6,
}


def _js_weekday(dt: datetime) -> int:
    """Returns weekday in JS convention: Sun=0, Mon=1, ..., Sat=6."""
    return dt.isoweekday() % 7


def _now_in_myt() -> datetime:
    """Returns current datetime as a naive local MYT datetime (no tzinfo)."""
    return datetime.now(tz=MYT).replace(tzinfo=None)  # strip pytz tzinfo → naive local


def _format_naive(dt: datetime) -> str:
    """Formats a naive datetime as 'YYYY-MM-DDTHH:MM:SS' with no offset suffix."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _time_to_mins(t: str) -> int:
    """Converts 'HH:MM' to total minutes since midnight."""
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _slot_date_times(slot: ClassSlot) -> tuple[str, str]:
    """
    Returns (start_str, end_str) as naive MYT datetime strings for a class slot,
    anchored to the next occurrence of slot.day strictly after today.
    """
    now_myt = _now_in_myt()
    base = now_myt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    target_js_day = JS_DAY_INDEX[slot.day]
    base_js_day = _js_weekday(base)
    days_until = (target_js_day - base_js_day + 7) % 7
    base = base + timedelta(days=days_until)

    sh, sm = (int(x) for x in slot.start.split(":"))
    duration_mins = (_time_to_mins(slot.end) - _time_to_mins(slot.start) + 24 * 60) % (24 * 60)

    start_dt = base.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_dt = start_dt + timedelta(minutes=duration_mins)

    return _format_naive(start_dt), _format_naive(end_dt)


async def create_weekly_class_events(
    creds: Credentials,
    student_name: str,
    schedule: list[ClassSlot],
) -> dict:
    """
    Creates weekly recurring Calendar events for each class slot.
    Returns {"meet_link": str, "event_count": int, "event_ids": list[str]}.
    """
    if not schedule:
        raise ValueError("Student has no class schedule.")

    calendar_id = settings.google_calendar_id
    loop = asyncio.get_event_loop()
    service = build_calendar(creds)

    first_slot = schedule[0]
    by_day_0 = BYDAY.get(first_slot.day)
    if not by_day_0:
        raise ValueError(f"Unknown day: {first_slot.day}")
    start_0, end_0 = _slot_date_times(first_slot)

    request_id = f"{student_name}-{first_slot.day}-{int(time.time())}-{uuid4().hex[:8]}"
    first_body = {
        "summary": student_name,
        "start": {"dateTime": start_0, "timeZone": "Asia/Kuala_Lumpur"},
        "end": {"dateTime": end_0, "timeZone": "Asia/Kuala_Lumpur"},
        "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={by_day_0}"],
        "conferenceData": {
            "createRequest": {
                "requestId": request_id,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    first_res = await loop.run_in_executor(
        None,
        lambda: service.events()
        .insert(calendarId=calendar_id, conferenceDataVersion=1, body=first_body)
        .execute(),
    )

    meet_link = first_res.get("hangoutLink")
    if not meet_link:
        raise RuntimeError("Calendar event created but no Meet link was returned.")
    first_id = first_res.get("id")
    if not first_id:
        raise RuntimeError("Calendar event created but no event ID was returned.")

    event_ids = [first_id]

    async def _create_remaining(slot: ClassSlot) -> str:
        by_day = BYDAY.get(slot.day)
        if not by_day:
            raise ValueError(f"Unknown day: {slot.day}")
        start_s, end_s = _slot_date_times(slot)
        body = {
            "summary": student_name,
            "description": f"Google Meet link: {meet_link}",
            "start": {"dateTime": start_s, "timeZone": "Asia/Kuala_Lumpur"},
            "end": {"dateTime": end_s, "timeZone": "Asia/Kuala_Lumpur"},
            "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={by_day}"],
        }
        res = await loop.run_in_executor(
            None,
            lambda: service.events()
            .insert(calendarId=calendar_id, conferenceDataVersion=0, body=body)
            .execute(),
        )
        eid = res.get("id")
        if not eid:
            raise RuntimeError("Calendar event created but no event ID was returned.")
        return eid

    remaining_ids = await asyncio.gather(*[_create_remaining(s) for s in schedule[1:]])
    event_ids.extend(remaining_ids)

    return {"meet_link": meet_link, "event_count": len(schedule), "event_ids": event_ids}


async def find_recurring_event_ids(
    creds: Credentials,
    student_name: str,
) -> list[str]:
    """
    Searches Calendar for recurring events matching student_name over the next
    90 days and returns deduplicated series-master event IDs.
    """
    calendar_id = settings.google_calendar_id
    loop = asyncio.get_event_loop()
    service = build_calendar(creds)

    now = datetime.now(tz=MYT)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=90)).isoformat()

    res = await loop.run_in_executor(
        None,
        lambda: service.events()
        .list(
            calendarId=calendar_id,
            q=student_name,
            singleEvents=True,
            timeMin=time_min,
            timeMax=time_max,
            maxResults=200,
            orderBy="startTime",
        )
        .execute(),
    )

    series_ids: set[str] = set()
    for event in res.get("items", []):
        if event.get("summary") == student_name and event.get("recurringEventId"):
            series_ids.add(event["recurringEventId"])

    return list(series_ids)


async def update_weekly_class_events(
    creds: Credentials,
    student_name: str,
    schedule: list[ClassSlot],
    existing_event_ids: list[str],
    meet_link: str,
) -> dict:
    """
    Nuke-and-repave strategy: find the primary event (owns the Meet conference),
    patch it for schedule[0], delete all others, create fresh for schedule[1:].
    Returns {"event_ids": list[str], "meet_link": str|None}.
    """
    if not schedule:
        raise ValueError("Student has no class schedule.")

    calendar_id = settings.google_calendar_id
    loop = asyncio.get_event_loop()
    service = build_calendar(creds)

    # Fetch all existing events in parallel; catch errors → None
    async def _fetch_event(eid: str):
        try:
            return await loop.run_in_executor(
                None,
                lambda: service.events().get(calendarId=calendar_id, eventId=eid).execute(),
            )
        except Exception:
            return None

    event_details = await asyncio.gather(*[_fetch_event(eid) for eid in existing_event_ids])

    # Primary = event with hangoutLink (owns the Meet conference)
    primary = next(
        (d for d in event_details if d and d.get("hangoutLink") and d.get("id")),
        None,
    )
    primary_id: str | None = primary["id"] if primary else None
    all_existing_ids = [d["id"] for d in event_details if d and d.get("id")]

    slot0 = schedule[0]
    by_day_0 = BYDAY.get(slot0.day)
    if not by_day_0:
        raise ValueError(f"Unknown day: {slot0.day}")
    start_0, end_0 = _slot_date_times(slot0)

    new_meet_link: str | None = None
    if primary_id:
        patch_body = {
            "summary": student_name,
            "start": {"dateTime": start_0, "timeZone": "Asia/Kuala_Lumpur"},
            "end": {"dateTime": end_0, "timeZone": "Asia/Kuala_Lumpur"},
            "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={by_day_0}"],
        }
        res = await loop.run_in_executor(
            None,
            lambda: service.events()
            .patch(calendarId=calendar_id, eventId=primary_id, body=patch_body)
            .execute(),
        )
        primary_result_id: str = res.get("id") or primary_id
    else:
        # Primary was deleted — create fresh with conferenceData, new Meet link
        insert_body = {
            "summary": student_name,
            "start": {"dateTime": start_0, "timeZone": "Asia/Kuala_Lumpur"},
            "end": {"dateTime": end_0, "timeZone": "Asia/Kuala_Lumpur"},
            "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={by_day_0}"],
            "conferenceData": {
                "createRequest": {
                    "requestId": f"{student_name}-sync-{int(time.time())}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        }
        res = await loop.run_in_executor(
            None,
            lambda: service.events()
            .insert(calendarId=calendar_id, conferenceDataVersion=1, body=insert_body)
            .execute(),
        )
        eid = res.get("id")
        if not eid:
            raise RuntimeError("Calendar event created but no event ID was returned.")
        primary_result_id = eid
        new_meet_link = res.get("hangoutLink") or None

    effective_meet_link = new_meet_link or meet_link

    # Delete non-primary existing events and create new ones for schedule[1:] in parallel
    ids_to_delete = [eid for eid in all_existing_ids if eid != primary_id]

    async def _delete_event(event_id: str) -> None:
        try:
            await loop.run_in_executor(
                None,
                lambda: service.events()
                .delete(calendarId=calendar_id, eventId=event_id)
                .execute(),
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "resp", None), "status", None
            )
            if status not in (404, 410):
                print(f"Failed to delete calendar event {event_id}: {exc}")

    async def _create_slot(slot: ClassSlot) -> str:
        by_day = BYDAY.get(slot.day)
        if not by_day:
            raise ValueError(f"Unknown day: {slot.day}")
        start_s, end_s = _slot_date_times(slot)
        body = {
            "summary": student_name,
            "description": f"Google Meet link: {effective_meet_link}",
            "start": {"dateTime": start_s, "timeZone": "Asia/Kuala_Lumpur"},
            "end": {"dateTime": end_s, "timeZone": "Asia/Kuala_Lumpur"},
            "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={by_day}"],
        }
        r = await loop.run_in_executor(
            None,
            lambda: service.events()
            .insert(calendarId=calendar_id, conferenceDataVersion=0, body=body)
            .execute(),
        )
        new_id = r.get("id")
        if not new_id:
            raise RuntimeError("Calendar event created but no event ID was returned.")
        return new_id

    delete_coros = [_delete_event(eid) for eid in ids_to_delete]
    create_coros = [_create_slot(s) for s in schedule[1:]]

    results = await asyncio.gather(*delete_coros, *create_coros, return_exceptions=True)

    # Collect newly created IDs (last len(schedule)-1 results are from create_coros)
    remaining_ids: list[str] = []
    create_results = results[len(delete_coros):]
    for r in create_results:
        if isinstance(r, Exception):
            raise r
        remaining_ids.append(r)  # type: ignore[arg-type]

    return {
        "event_ids": [primary_result_id] + remaining_ids,
        "meet_link": new_meet_link,
    }
