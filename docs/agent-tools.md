# Agent Tool Reference

All 18 shared tools available to the AI agent at `/admin/agent`. The same tool logic is shared by both agent backends:

| Backend | Schema format | Entry point |
|---|---|---|
| **Classic** | `FunctionDeclaration[]` built in `app/features/agent/schema.py` | `POST /agent/chat` |
| **LangGraph** (default) | LangGraph `tool()` wrappers with Pydantic schemas in `app/features/agent/lg/tool_factories.py`; single-turn supervisor dispatches to specialist subagents in parallel via `Send` | `POST /agent/lg/chat` |

Tool implementations live in `app/features/agent/tools/` (split by domain: `student_tools.py`, `template_tools.py`, `timetable_tools.py`) and are called by both backends.

**Both backends are stateless** — history is sent from the client on every request. The server returns the updated history in a `{ type: 'history' }` / `{ type: 'lg_history' }` SSE event before `done`. For LangGraph, `lg_history` contains only routing-level messages — subagent-internal tool call pairs are stripped server-side by `is_routing_relevant` before emission. See `docs/decisions.md` for the full rationale.

**LangSmith tracing** — LangGraph runs are traced automatically when `LANGCHAIN_TRACING=true` and `LANGSMITH_API_KEY` are set. Traces appear in the LangSmith web UI under the `LANGSMITH_PROJECT` name (default: `tuition-agent`). Classic-mode runs are not traced. See `.env.example` for the full set of LangSmith env vars.

---

## Table of Contents

Tools are grouped by domain. In LangGraph mode each domain maps to one subagent; in classic mode all 18 are available to the single agent.

**`student_agent` / classic** (10 tools)

| Tool | Type | Summary |
|---|---|---|
| [`search_students`](#search_students) | Read | Find students by name |
| [`get_student`](#get_student) | Read | Fetch all fields for one student |
| [`list_students`](#list_students) | Read | List all students with optional status filter |
| [`create_student`](#create_student) | Write | Create a new student record |
| [`update_student`](#update_student) | Write | Update one or more fields on a student |
| [`delete_student`](#delete_student) | Write | Permanently delete a student |
| [`sync_all_students`](#sync_all_students) | Write | Sync all active students' Calendar + Drive to match DB |
| [`manage_portal_access`](#manage_portal_access) | Write | Add/remove a portal login email for a student |
| [`get_schedule`](#get_schedule) | Read | List students who have class on a given day |
| [`get_fee_summary`](#get_fee_summary) | Read | Calculate monthly fee revenue across all active students |

**`template_agent` / classic** (3 tools)

| Tool | Type | Summary |
|---|---|---|
| [`list_templates`](#list_templates) | Read | List all template IDs, titles, and descriptions |
| [`get_template`](#get_template) | Read | Fetch the full content of a single template |
| [`generate_payment_message`](#generate_payment_message) | Read | Generate a ready-to-send payment reminder message |

**`timetable_agent` / classic** (5 tools)

| Tool | Type | Summary |
|---|---|---|
| [`get_timetable_settings`](#get_timetable_settings) | Read | Read scheduling rules and buffer minutes |
| [`update_timetable_rules`](#update_timetable_rules) | Write | Save new scheduling rules text |
| [`update_buffer_mins`](#update_buffer_mins) | Write | Save a new buffer duration between classes |
| [`generate_slot_availability`](#generate_slot_availability) | Read | AI-classify every free 30-min slot as preferred / normal / unavailable |
| [`download_timetable_image`](#download_timetable_image) | Read | Fetch active students for a weekly schedule PNG download |

---

## `search_students`

Search for students by name (partial, case-insensitive).

### Input

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `query` | string | Yes | Partial or full student name |

### Process

Runs a Supabase `ilike` query (`%query%`) against the `name` column. Returns `id`, `name`, `status`, `class_schedule` only — not the full record. Ordered alphabetically by name.

### Output

**Success**
```json
{
  "students": [
    { "id": "uuid", "name": "Alice", "status": "Active", "class_schedule": [...] }
  ]
}
```

**Error**
```json
{ "error": "string" }
```

---

## `get_student`

Fetch every field for a single student by UUID.

### Input

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `id` | string (UUID) | Yes | Obtain from `search_students` first |

### Process

Single Supabase `select` with `.eq('id', id).maybeSingle()`. Returns all columns including Google links, portal access emails, and calendar event IDs.

### Output

**Success**
```json
{
  "student": {
    "id": "uuid",
    "name": "Alice",
    "status": "Active",
    "mode": "My Python Syllabus",
    "fee_per_hour": 80,
    "payment_method": "Monthly",
    "class_schedule": [{ "day": "Monday", "start": "15:00", "end": "17:00" }],
    "contact_person": "Mum",
    "contact_phone": "601x-xxxxxxx",
    "student_phone": null,
    "today_homework": "Finish exercise 3",
    "notes": null,
    "latest_payment": "2026-04",
    "google_meet_link": "https://meet.google.com/...",
    "google_drive_link": "https://drive.google.com/...",
    "calendar_event_ids": ["event-id-1"],
    "access_emails": ["parent@example.com"]
  }
}
```

**Error**
```json
{ "error": "Student not found" }
```

---

## `list_students`

List students with an optional status filter.

### Input

| Parameter | Type | Required | Default | Notes |
|---|---|---|---|---|
| `status` | `"Active"` \| `"On Hold"` \| `"Completed"` | No | All statuses | |

### Process

Supabase `select` returning `id`, `name`, `status`, `mode`, `fee_per_hour`, `class_schedule`. Ordered by name. If `status` is supplied, filters with `.eq('status', status)`.

### Output

**Success**
```json
{
  "students": [
    {
      "id": "uuid",
      "name": "Alice",
      "status": "Active",
      "mode": "My Python Syllabus",
      "fee_per_hour": 80,
      "class_schedule": [...]
    }
  ]
}
```

**Error**
```json
{ "error": "Invalid status: ..." }
```

---

## `create_student`

Create a new student record in the database.

### Input

| Parameter | Type | Required | Default | Notes |
|---|---|---|---|---|
| `name` | string | Yes | | |
| `mode` | `"My Python Syllabus"` \| `"Other Syllabus"` | Yes | | |
| `fee_per_hour` | number | Yes | | RM per hour |
| `payment_method` | `"Monthly"` \| `"Weekly"` | No | `"Monthly"` | |
| `status` | `"Active"` \| `"On Hold"` \| `"Completed"` | No | `"Active"` | |
| `class_schedule` | `ClassSlot[]` | No | `[]` | Array of `{ day, start, end }` |
| `contact_person` | string | No | null | |
| `contact_phone` | string | No | null | |
| `student_phone` | string | No | null | |
| `today_homework` | string | No | null | |
| `notes` | string | No | null | |
| `latest_payment` | string | No | null | e.g. `"2026-05"` |
| `access_emails` | string[] | No | `[]` | Portal login emails |
| `google_meet_link` | string | No | null | |
| `google_drive_link` | string | No | null | |

### Process

Single Supabase `insert`. Returns `{ id, name }` on success.

### Output

**Success**
```json
{ "id": "uuid", "name": "Alice" }
```

**Success with warning**
```json
{ "id": "uuid", "name": "Alice", "google_warning": "..." }
```

**Error**
```json
{ "error": "string" }
```

---

## `update_student`

Update one or more fields on an existing student.

### Input

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `id` | string (UUID) | Yes | |
| `fields` | object | Yes | Keys must be from the allowlist below |

**Allowed field keys:** `name`, `mode`, `fee_per_hour`, `payment_method`, `status`, `class_schedule`, `contact_person`, `contact_phone`, `student_phone`, `today_homework`, `notes`, `latest_payment`, `google_meet_link`, `google_drive_link`, `access_emails`

Fields not in this allowlist are silently stripped (prevents prompt injection).

### Process

1. Strips disallowed keys from `fields`
2. Normalises `access_emails` entries to lowercase+trimmed if present
3. Runs Supabase `update`
4. If `class_schedule` was updated **and** the student has `calendar_event_ids` + `google_meet_link`: calls `update_weekly_class_events` (nuke-and-repave) and `update_student_meet_doc` in parallel via `asyncio.gather`; if a new Meet link is generated (primary was deleted), also saves it to DB and re-updates the Drive doc; Google failures are non-fatal and returned as `googleWarnings`

### Output

**Success**
```json
{ "success": true }
```

**Success with Google warnings**
```json
{
  "success": true,
  "googleWarnings": ["Calendar update failed: ...", "Drive Meet doc update failed: ..."]
}
```

**Error**
```json
{ "error": "string" }
```

---

## `delete_student`

Permanently delete a student record and clean up Google resources.

> **Safety:** the agent will not call this without seeing "yes" in the conversation. It will warn the user that Calendar events and the Drive folder will also be removed.

### Input

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `id` | string (UUID) | Yes | |

### Process

1. Fetches `google_drive_link` and `calendar_event_ids` from DB
2. If Google resources exist: moves Drive folder to Trash and deletes all Calendar events in parallel via `asyncio.gather` (failures are non-fatal — recorded as warnings)
3. Runs Supabase `delete`

### Output

**Success**
```json
{ "success": true }
```

**Success with Google warnings**
```json
{
  "success": true,
  "warnings": ["Drive cleanup warning: ...", "Calendar cleanup warning: ..."]
}
```

**Error**
```json
{ "error": "string" }
```

---

## `sync_all_students`

Sync every active student's Google Calendar events and Drive "Google Meet Link" doc to match the current DB schedule.

> **Safety:** the agent requires explicit confirmation before calling this — it affects every active student.

### Input

None.

### Process

1. Fetches OAuth credentials from `settings` table via `get_oauth2_credentials()`
2. For **every** active student: searches Calendar by exact student name (`find_recurring_event_ids`) and merges discovered IDs with any stored `calendar_event_ids` — catches rogue events not tracked in the DB
3. Applies nuke-and-repave via `update_weekly_class_events`: finds the event that owns the Meet conference, patches it to slot 0, deletes all others, creates fresh events for remaining slots
4. If the primary event was deleted, a new Meet link is generated — saved to DB and Drive doc automatically
5. All students processed in parallel via `asyncio.gather`
6. If `invalid_grant` is detected, stops early and returns a reconnect message

### Output

**Success**
```json
{
  "results": [
    { "name": "Alice", "status": "synced" },
    { "name": "Bob", "status": "skipped", "reason": "no calendar events" },
    { "name": "Carol", "status": "error", "reason": "..." }
  ]
}
```

**Auth error**
```json
{ "error": "Google auth expired — reconnect at /api/google/auth" }
```

---

## `manage_portal_access`

Add or remove an email address from a student's portal login list (`access_emails`).

### Input

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `student_id` | string (UUID) | Yes | |
| `action` | `"add"` \| `"remove"` | Yes | |
| `email` | string | Yes | Normalised to lowercase + trimmed |

### Process

1. Fetches current `access_emails` array from DB
2. For `add`: appends the normalised email if not already present
3. For `remove`: filters out the normalised email
4. Runs Supabase `update` with the new array

### Output

**Success**
```json
{ "result": "parent@example.com can now log in to the student portal" }
```

```json
{ "result": "parent@example.com has been removed from portal access" }
```

**No-op**
```json
{ "result": "parent@example.com already has access" }
```

**Error**
```json
{ "error": "Student not found" }
```

---

## `get_schedule`

Get the list of students who have class on a specific day of the week.

### Input

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `day` | `"Monday"` … `"Sunday"` | Yes | |

### Process

Fetches all active students with `id`, `name`, `class_schedule`. Maps each to `{ id, name, slots }` where `slots` is the subset of `class_schedule` entries matching the requested day. Filters out students with no matching slots.

### Output

**Success**
```json
{
  "day": "Monday",
  "students": [
    { "id": "uuid", "name": "Alice", "slots": [{ "start": "15:00", "end": "17:00" }] }
  ]
}
```

`students` is an empty array if no one has class that day.

**Error**
```json
{ "error": "string" }
```

---

## `get_fee_summary`

Calculate monthly tuition fee revenue across all active students.

### Input

| Parameter | Type | Required | Default | Notes |
|---|---|---|---|---|
| `month` | number (1–12) | No | Current month (MYT) | |
| `year` | number | No | Current year (MYT) | |

### Process

1. Fetches all active students: `id`, `name`, `fee_per_hour`, `class_schedule`
2. For each student, groups slots by day via `groupSlotsByDay`, then calls `getWeekdayDates` to find every occurrence of that weekday in the target month
3. Fee per student = Σ (session_count × hours_per_session × fee_per_hour) across all days
4. Raw fees are tracked in a parallel array before rounding to avoid per-student accumulation errors; the total is rounded once at the end

### Output

**Success**
```json
{
  "month": 6,
  "year": 2026,
  "students": [
    { "id": "uuid", "name": "Alice", "fee": 320 },
    { "id": "uuid", "name": "Bob", "fee": 240 }
  ],
  "total": 560
}
```

**Error**
```json
{ "error": "string" }
```

---

## `list_templates`

List all message template IDs, titles, and descriptions. Does not return content.

### Input

None.

### Process

Pure function — no DB call. Reads from the in-memory `TEMPLATE_META` dict in `app/features/templates/service.py`.

### Output

```json
{
  "templates": [
    { "id": "payment", "title": "Payment Reminder 1", "description": "Standard monthly fee reminder" },
    { "id": "payment2", "title": "Payment Reminder 2", "description": "With carryover session deduction" },
    { "id": "review_request1", "title": "Review Request 1", "description": "..." },
    { "id": "first_approach", "title": "First Approach", "description": "Superprof outreach message" }
  ]
}
```

---

## `get_template`

Fetch the full content of a single message template.

### Input

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `id` | string | Yes | Must be a valid template ID — use `list_templates` to discover IDs |

### Process

Supabase `select` with `.eq('id', id).maybeSingle()`. Merges DB `content` with in-memory `TEMPLATE_META` to return title and description alongside the content.

### Output

**Success**
```json
{
  "template": {
    "id": "payment",
    "title": "Payment Reminder 1",
    "description": "Standard monthly fee reminder",
    "content": "Hi {name}, just a gentle reminder..."
  }
}
```

**Error**
```json
{ "error": "Template \"payment\" not found" }
```

---

## `generate_payment_message`

Generate a ready-to-send payment reminder message for a student. Calculates session dates and total fee automatically from the student's schedule and fee rate.

> **LangGraph enforcement:** `template_agent`'s system prompt requires it to ALWAYS call this tool — it is prohibited from writing payment message content itself. This ensures the message contains real session dates and fees computed from the student's DB record, never hallucinated content.

### Input

| Parameter | Type | Required | Default | Notes |
|---|---|---|---|---|
| `student_id` | string (UUID) | Yes | | |
| `month` | number (1–12) | No | Next month (MYT) | |
| `year` | number | No | Next month's year (MYT) | |
| `template_type` | `1` \| `2` | No | `1` | 1 = standard, 2 = with carryover deduction |
| `carryover` | number | No | `0` | Sessions to deduct; only meaningful for `template_type 2` |

### Process

1. Resolves month/year — defaults to next calendar month in MYT if not supplied
2. Fetches student: `name`, `contact_person`, `class_schedule`, `fee_per_hour`, `status`
3. Validates student is Active
4. Delegates all calculation and message building to `build_payment_message()` from `app/features/payment/service.py`

`buildPaymentMessage()` internally:
- Groups slots by day; calls `getWeekdayDates` for each day to find every occurrence in the target month
- Collects all session dates, sorts them, computes `sessionFeeTotal`
- Resolves `recipient` — uses `contact_person` if set and not `"-"`, otherwise falls back to `name`
- **Template 1:** `"Hi {recipient}, … {N} sessions in {month} ({dates}), bringing the total to RM{fee}. Thank you 😄"`
- **Template 2:** deducts `carryover × avg_fee_per_session` from total: `"Hi {recipient}, … With {N} session(s) carried over …, bringing the total to RM{adjusted_fee}. Thank you. 😄"`

### Output

**Success**
```json
{
  "message": "Hi Mum, just a gentle reminder regarding the tuition fee. There are 4 sessions in June (2nd, 9th, 16th, and 23rd), bringing the total to RM320. Thank you 😄",
  "month": 6,
  "year": 2026,
  "monthName": "June"
}
```

**Error**
```json
{ "error": "Student not found" }
{ "error": "Student is not active" }
{ "error": "No scheduled class days found for this student" }
```

---

## `get_timetable_settings`

Read the current scheduling rules and buffer duration from the database. Call this before `update_timetable_rules` or `update_buffer_mins` to show the user the current values.

### Input

None.

### Process

Fetches `timetable_rules` and `timetable_buffer_mins` from the `settings` table in parallel via `asyncio.gather`. If `timetable_buffer_mins` is not set, defaults to `15`.

### Output

```json
{
  "rules": "Prefer Mon/Tue/Thu/Fri. No slots before 9am or after 9pm.",
  "bufferMins": 15
}
```

---

## `update_timetable_rules`

Save new scheduling rules text to the database. These rules are passed verbatim to Gemini when generating slot availability.

> **Safety:** the agent shows the user the proposed rules and confirms before calling this.

### Input

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `rules` | string | Yes | Full scheduling rules text to save |

### Process

Supabase `upsert` into `settings` with `key = 'timetable_rules'` and `onConflict: 'key'`. After the loop, `selfEval` reads the row back and compares to confirm the write persisted.

### Output

**Success**
```json
{ "ok": true }
```

**Error**
```json
{ "error": "string" }
```

---

## `update_buffer_mins`

Save a new buffer duration to the database. Buffer zones are computed in code and block the slots immediately before and after each booked class.

### Input

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `buffer_mins` | number | Yes | Minutes of buffer around booked classes (0–60) |

### Process

Validates `0 ≤ buffer_mins ≤ 60` — returns `{ error }` immediately if out of range. Supabase `upsert` into `settings` with `key = 'timetable_buffer_mins'`, storing the value as a string. After the loop, `selfEval` reads the row back and parses the integer to confirm.

### Output

**Success**
```json
{ "ok": true }
```

**Error**
```json
{ "error": "bufferMins must be 0–60" }
```

---

## `generate_slot_availability`

Run the AI slot-availability generator. Reads scheduling rules, buffer minutes, and all active students' class schedules from the database, then calls Gemini 2.5 Flash to classify every free 30-minute slot.

After the tool completes, the route emits a `ui_action` SSE event (`action: "slots_ready"`) and a **Download Slot Availability PNG** button appears in the chat.

### Input

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `student_availability` | string | No | Free-text description of when a prospective student can attend. Example: `"free Tuesday and Thursday after 4pm"` |

### Process

1. Fetches `timetable_rules`, `timetable_buffer_mins`, and all active students' `class_schedule` in a single `asyncio.gather`
2. Returns `{ error }` immediately if no rules are configured
3. Calls `run_gemini_slot_generation` from `app/shared/gemini/slot_generation.py`:
   - Computes buffer zones in code via `compute_buffer_slots`
   - Builds the classifiable slot list (non-booked, non-buffered) via `build_booked_cell_set`
   - Sends prompt to Gemini with structured JSON output schema
   - Post-processes: forces any buffer slot that sneaks through to `unavailable`
4. Returns `{ slots }` — the route emits `{ type: 'ui_action', action: 'slots_ready', payload: { slots } }` as an SSE event

### Output

**Success**
```json
{
  "slots": [
    { "day": "Monday", "time": "09:00", "state": "preferred" },
    { "day": "Monday", "time": "09:30", "state": "normal" },
    { "day": "Monday", "time": "10:00", "state": "unavailable" }
  ]
}
```

**Error**
```json
{ "error": "No timetable rules configured. Use update_timetable_rules first." }
{ "error": "Slot generation failed: ..." }
```

---

## `download_timetable_image`

Fetch all active students and their schedules so the frontend can render a weekly schedule PNG client-side.

After the tool completes, the route emits a `ui_action` SSE event (`action: "download_schedule"`) and a **Download Schedule PNG** button appears in the chat. The PNG is rendered in the browser using the same `drawScheduleToCtx` function as the timetable tab, producing pixel-identical output.

### Input

None.

### Process

Supabase `select('name, class_schedule')` on `students` where `status = 'Active'`, ordered by name. Maps rows to `{ name, class_schedule }`. Returns `{ students }` — the route emits `{ type: 'ui_action', action: 'download_schedule', payload: { students } }` as an SSE event.

### Output

**Success**
```json
{
  "students": [
    {
      "name": "Alice",
      "class_schedule": [{ "day": "Monday", "start": "15:00", "end": "17:00" }]
    }
  ]
}
```

**Error**
```json
{ "error": "string" }
```

---

## LangGraph-only tools

These tools are **not** available in the classic Gemini loop. `dispatch` belongs to the supervisor; `final_answer` and `cannot_complete` belong to every subagent.

### `dispatch`

The supervisor's sole routing tool, created by `create_dispatch_tool()` in `lg/handoff.py`. Routes one or more tasks to specialist subagents — all entries in a single call execute in parallel.

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `handoffs` | `HandoffEntry[]` | Yes | At least one entry required |

**`HandoffEntry` fields:**

| Field | Type | Notes |
|---|---|---|
| `agentName` | string | One of `student_agent`, `template_agent`, `timetable_agent` |
| `task` | string | Self-contained task description — resolve all ambiguities (dates, names) before dispatching |

The tool function itself never executes — `supervisor_node` intercepts the tool call and emits `Send` commands directly (one per entry, or merged for same-agent entries). Only the schema is needed so the LLM knows the interface.

---

## LangGraph-only terminal tools

These two tools are appended to every subagent's tool list by `make_student_tools()`, `make_template_tools()`, and `make_timetable_tools()` in `lg/tool_factories.py`. They are **not** available in the classic Gemini loop — they exist solely to give LangGraph subagents a structured way to end their turn.

### `final_answer`

Signal that the subagent has finished all tool calls and is ready to return a reply to the supervisor.

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `text` | string | Yes | Complete reply — all formatted content (tables, bold labels, `[student_id:NAME:UUID]` tokens) goes inside this parameter |

The subagent **must** call this as the last tool of every successful turn. `route_after_tools` in `lg/subagent.py` detects `final_answer` in `TERMINAL_TOOLS` and routes to `END`, skipping the next agent LLM call.

### `cannot_complete`

Signal that the assigned task cannot be completed with the subagent's available tools (e.g. a student task routed to the template agent).

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `reason` | string | Yes | Clear explanation of why the task cannot be completed |

Triggers the same `END` routing as `final_answer`. The supervisor's `make_call_agent` helper checks for `final_answer`/`cannot_complete` ToolMessages first, falling back to a free-text AIMessage if neither is present.
