import asyncio
import time
from datetime import datetime, timedelta
from functools import partial

import pytz
import requests as _req
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

from app.config import settings
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

JS_DAY_INDEX: dict[str, int] = {
    "Sunday": 0,
    "Monday": 1,
    "Tuesday": 2,
    "Wednesday": 3,
    "Thursday": 4,
    "Friday": 5,
    "Saturday": 6,
}

_CAL = "https://www.googleapis.com/calendar/v3/calendars"


def _session(creds: Credentials) -> AuthorizedSession:
    """Requests session that bypasses system proxy (thread-safe, no httplib2 SSL issues)."""
    s = AuthorizedSession(creds)
    s.trust_env = False
    return s


def _js_weekday(dt: datetime) -> int:
    return dt.isoweekday() % 7


def _now_in_myt() -> datetime:
    return datetime.now(tz=MYT).replace(tzinfo=None)


def _format_naive(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _time_to_mins(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _slot_date_times(slot: ClassSlot) -> tuple[str, str]:
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


# ---------------------------------------------------------------------------
# Sync helpers (each creates its own session so threads don't share state)
# ---------------------------------------------------------------------------

def _insert_event(creds: Credentials, calendar_id: str, body: dict) -> dict:
    s = _session(creds)
    resp = s.post(f"{_CAL}/{calendar_id}/events", params={"conferenceDataVersion": 1}, json=body)
    resp.raise_for_status()
    return resp.json()


def _insert_event_no_conf(creds: Credentials, calendar_id: str, body: dict) -> dict:
    s = _session(creds)
    resp = s.post(f"{_CAL}/{calendar_id}/events", json=body)
    resp.raise_for_status()
    return resp.json()


def _patch_event(creds: Credentials, calendar_id: str, event_id: str, body: dict) -> dict:
    s = _session(creds)
    resp = s.patch(f"{_CAL}/{calendar_id}/events/{event_id}", json=body)
    resp.raise_for_status()
    return resp.json()


def _get_event(creds: Credentials, calendar_id: str, event_id: str) -> dict | None:
    s = _session(creds)
    resp = s.get(f"{_CAL}/{calendar_id}/events/{event_id}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _delete_event(creds: Credentials, calendar_id: str, event_id: str) -> None:
    s = _session(creds)
    resp = s.delete(f"{_CAL}/{calendar_id}/events/{event_id}")
    if resp.status_code in (404, 410):
        return
    resp.raise_for_status()


def _list_events(creds: Credentials, calendar_id: str, **params) -> dict:
    s = _session(creds)
    resp = s.get(f"{_CAL}/{calendar_id}/events", params=params)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

async def create_weekly_class_events(
    creds: Credentials,
    student_name: str,
    schedule: list[ClassSlot],
) -> dict:
    """Creates weekly recurring Calendar events. Returns {meet_link, event_count, event_ids}."""
    if not schedule:
        raise ValueError("Student has no class schedule.")

    calendar_id = settings.google_calendar_id
    loop = asyncio.get_running_loop()

    first_slot = schedule[0]
    by_day_0 = BYDAY.get(first_slot.day)
    if not by_day_0:
        raise ValueError(f"Unknown day: {first_slot.day}")
    start_0, end_0 = _slot_date_times(first_slot)

    request_id = f"{student_name}-{first_slot.day}-{int(time.time())}"
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
        None, partial(_insert_event, creds, calendar_id, first_body)
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
            None, partial(_insert_event_no_conf, creds, calendar_id, body)
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
    """Searches Calendar over 90 days and returns deduplicated series-master event IDs."""
    calendar_id = settings.google_calendar_id
    loop = asyncio.get_running_loop()
    now = datetime.now(tz=MYT)

    res = await loop.run_in_executor(
        None,
        partial(
            _list_events,
            creds,
            calendar_id,
            q=student_name,
            singleEvents=True,
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=90)).isoformat(),
            maxResults=200,
            orderBy="startTime",
        ),
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
    meet_link: str | None = None,
) -> dict:
    """Nuke-and-repave. Returns {event_ids, meet_link: str|None, schedule_cleared?: bool}."""
    if not schedule:
        loop = asyncio.get_running_loop()
        calendar_id = settings.google_calendar_id
        if existing_event_ids:
            await asyncio.gather(
                *[loop.run_in_executor(None, partial(_delete_event, creds, calendar_id, eid))
                  for eid in existing_event_ids],
                return_exceptions=True,
            )
        return {"event_ids": [], "meet_link": None, "schedule_cleared": True}

    calendar_id = settings.google_calendar_id
    loop = asyncio.get_running_loop()

    event_details = await asyncio.gather(
        *[loop.run_in_executor(None, partial(_get_event, creds, calendar_id, eid))
          for eid in existing_event_ids],
        return_exceptions=True,
    )
    event_details = [d for d in event_details if isinstance(d, dict)]

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
            None, partial(_patch_event, creds, calendar_id, primary_id, patch_body)
        )
        primary_result_id: str = res.get("id") or primary_id
    else:
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
            None, partial(_insert_event, creds, calendar_id, insert_body)
        )
        eid = res.get("id")
        if not eid:
            raise RuntimeError("Calendar event created but no event ID was returned.")
        primary_result_id = eid
        new_meet_link = res.get("hangoutLink") or None

    effective_meet_link = (
        new_meet_link
        or (primary.get("hangoutLink") if primary else None)
        or meet_link
    )
    ids_to_delete = [eid for eid in all_existing_ids if eid != primary_id]

    async def _del(event_id: str) -> None:
        await loop.run_in_executor(
            None, partial(_delete_event, creds, calendar_id, event_id)
        )

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
            None, partial(_insert_event_no_conf, creds, calendar_id, body)
        )
        new_id = r.get("id")
        if not new_id:
            raise RuntimeError("Calendar event created but no event ID was returned.")
        return new_id

    results = await asyncio.gather(
        *[_del(eid) for eid in ids_to_delete],
        *[_create_slot(s) for s in schedule[1:]],
        return_exceptions=True,
    )

    del_results = results[: len(ids_to_delete)]
    create_results = results[len(ids_to_delete) :]

    failed = [str(r) for r in del_results if isinstance(r, Exception)]
    if failed:
        raise RuntimeError(f"Failed to delete calendar events: {'; '.join(failed)}")

    remaining_ids: list[str] = []
    for r in create_results:
        if isinstance(r, Exception):
            raise r
        remaining_ids.append(r)  # type: ignore[arg-type]

    return {
        "event_ids": [primary_result_id] + remaining_ids,
        "meet_link": new_meet_link,
        "effective_meet_link": effective_meet_link,
    }
