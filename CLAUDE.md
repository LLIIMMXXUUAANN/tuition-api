# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@claude/agent.md
@claude/timetable.md
@claude/google.md

## Documentation files

| File | Covers |
|---|---|
| `docs/decisions.md` | Non-obvious design decisions, Pydantic trust boundary patterns, and reasoning behind them |
| `claude/agent.md` | Tool contract, system rules, SSE contract, LangGraph graph |
| `claude/timetable.md` | Timetable routes, slot classification algorithm, prompt rules |
| `claude/google.md` | Google OAuth, Calendar, Drive, cleanup, and bulk sync implementation |
| `docs/agent-tools.md` | Full 18-tool reference (input / process / output for each tool) |

All frontend documentation (features, UI, routing, components, decisions) is in the frontend repo — see `../Tuition/README.md` and `../Tuition/CLAUDE.md`.

## Commands

```bash
uv run uvicorn app.main:app --reload   # dev server (auto-reload)
uv run pytest                          # run all tests
uv run pytest tests/test_agent.py -v  # single test file
uv run pytest tests/test_students.py::test_create_student -v  # single test
uv run ruff check .                   # lint
uv run ruff format .                  # format
uv run pyright                        # type-check
```

Tests require a valid `.env` — they spin up a real `TestClient` and hit real Supabase.

## Architecture

Every module is a port of the original TypeScript/Next.js backend (`src/features/...`), so module docstrings always reference their original file.

### Source structure

```
app/
  main.py              → FastAPI app, CORS, router mounts
  config.py            → Pydantic Settings (loaded from .env)
  auth.py              → require_internal_secret dependency
  types.py             → ClassSlot, Student, enums (StudentMode, StudentStatus, SlotState…)
  features/
    google/
      router.py        → Calendar/Drive CRUD + OAuth setup routes
      auth.py          → OAuth credentials, CSRF state tokens, token rotation
      calendar.py      → Recurring Calendar events (create / find / update)
      drive.py         → Student Drive folders + Meet docs
      cleanup.py       → Bulk delete on student removal
      sync.py          → Full sync for all active students
      errors.py        → friendly_google_error, auth_expired (shared error helpers)
    students/
      router.py        → Student CRUD + portal lookup routes
      service.py       → StudentNotFoundError, IdempotencyKeyConflictError, IdempotencyPayloadMismatchError, hash_payload, build_insert_data, create_student, update_student, delete_student
    payment/
      router.py        → Payment message generation route
      service.py       → build_payment_message wrapper
    timetable/
      router.py        → Timetable rules + buffer + slot generation routes
      service.py       → TimetableValidationError, save_rules, save_buffer_mins
    templates/
      router.py        → Message template read/update routes
      service.py       → TEMPLATE_META, template_meta(), get_template
    agent/
      router.py        → AI agent SSE — LangGraph multi-agent + conversation endpoints
      persistence.py   → conversation + message DB helpers (get_or_create_conversation, clear_conversation, pre_insert_agent_message, insert_user_message, update_agent_message)
      eval.py          → self_eval — post-mutation DB verification
      state.py         → stop_signals dict (keyed by request_id)
      tools/
        student_tools.py → 10 student + Google + portal tools
        template_tools.py → 3 template + payment-message tools
        timetable_tools.py → 5 timetable + slot-generation tools
      lg/
        agent_state.py   → AgentState (messages + audit_log)
        utils.py         → extract_text (shared text extraction helper)
        model.py         → get_gemini_chat_model (fresh ChatGoogle per call)
        handoff.py       → create_dispatch_tool, normalize_agent_name
        subagent.py      → build_subagent (ReAct graph)
        tool_factories.py → make_student/template/timetable_tools + terminal tools
        student_agent.py → make_student_agent
        template_agent.py → make_template_agent
        timetable_agent.py → make_timetable_agent
        supervisor.py    → make_supervisor, build_custom_supervisor
        post_hooks.py    → make_student_post_hook, make_timetable_post_hook
        stream_adapter.py → pipe_langgraph_stream, is_routing_relevant
  shared/
    db.py              → get_supabase (singleton), get_setting, get_active_students
    errors.py          → err_msg helper (shared by all tool files and google/sync)
    utils.py           → DAYS, TIME_SLOTS, time_to_mins, format_fee, get_weekday_dates, get_myt_now…
    schema.py          → CamelResponse shared response class
    gemini/
      client.py        → Singleton google.genai.Client
      slot_generation.py → run_gemini_slot_generation (structured JSON output)
```

### Entry point and routing

`app/main.py` mounts six routers:

| Prefix | Router | Purpose |
|---|---|---|
| `/google` | `app/features/google/router.py` | Google Calendar/Drive CRUD + OAuth |
| `/students` | `app/features/students/router.py` | Student CRUD + portal lookup |
| `/payment` | `app/features/payment/router.py` | Payment message generation |
| `/timetable` | `app/features/timetable/router.py` | Timetable rules and slot generation |
| `/agent` | `app/features/agent/router.py` | AI agent SSE streaming (LangGraph) |
| `/templates` | `app/features/templates/router.py` | Message template read/update |

All routers require the `X-Internal-Secret` header (checked by `app/auth.py`).

### Configuration

`app/config.py` — a single `Settings` Pydantic model loaded from `.env`. All settings are accessed via the singleton `settings` imported from this module.

### Supabase

`app/shared/db.py` — lazy singleton `AsyncClient`. Always obtained with `await get_supabase()`. Uses the service-role key (bypasses RLS). Google OAuth refresh token is stored in the `settings` table under key `google_refresh_token`.

### Database schema

Seven tables in the `public` schema:

- **`agent_conversations`** — one permanent row. Holds `lg_contents` and `prev_lg_contents` (both JSONB) — the LLM-level history used to reconstruct context. `prev_lg_contents` stores the state before the last successful turn, enabling one-level undo for latest-message edit. RLS: `is_tutor()` only.
- **`agent_messages`** — one row per message. Columns: `conversation_id`, `role` (`user` | `agent`), `content`, `steps` (JSONB array), `is_error` (bool), `students` (JSONB), `schedule_students` (JSONB), `slot_data` (JSONB), `created_at`. RLS: `is_tutor()` only.
- **`students`** — one row per student. `class_schedule` is a `jsonb` column storing `ClassSlot[]` (array of `{ day, start, end }`). `access_emails text[]` lists emails that can log in to the student portal. `calendar_event_ids text[]` stores one Google Calendar event ID per class slot, positionally matched to `class_schedule` (index 0 = event that owns the Meet conference). `status` (`Active` | `On Hold` | `Completed`) is the sole active/inactive flag. `today_homework` is a `text` column (multi-line). RLS: admin has full access; students can only SELECT their own row. Backend always uses the service-role key and bypasses RLS.
- **`templates`** — one row per template, keyed by text `id` (e.g. `payment`, `review_request1`, `first_approach`). `content` is upserted on Save.
- **`tutors`** — one row per tutor email. Accessed only via SECURITY DEFINER functions.
- **`settings`** — key/value store. Keys: `google_refresh_token`, `timetable_rules`, `timetable_buffer_mins` (integer stored as string, default `'15'`).
- **`idempotency_keys`** — one row per in-flight/completed idempotent request, keyed by the client-supplied `Idempotency-Key`. Columns: `endpoint`, `request_hash` (detects the same key reused with a different body), `status` (`pending` | `completed`), `response_status`, `response_body` (JSONB, replayed verbatim on retry), `resource_id` (FK to `students.id`, lets a stale-pending key resume against the row it already created instead of inserting a duplicate). Backing store for the `create_student_idempotent()` RPC used by `POST /students` — see `docs/decisions.md`, "Idempotency-Key — POST /students (atomic RPC)". No RLS policy — only touched by that `SECURITY DEFINER` function and the backend's own follow-up completion `UPDATE`.

SECURITY DEFINER functions (called from the frontend, not the backend):
- `is_tutor()` — returns true if `auth.email()` is in `tutors`
- `check_tutor_access(p_email)` — returns true if given email is in `tutors`
- `check_portal_access(p_email)` — returns true if given email is in any student's `access_emails`

### Shared types

`app/types.py` — canonical `ClassSlot` and `Student` Pydantic models used across routers and tools.

### Service layer pattern

Each feature has a `service.py` that owns domain logic and raises typed exceptions:

- **Domain exceptions** (e.g. `StudentNotFoundError`, `TimetableValidationError`) are raised by the service.
- **HTTP routers** wrap every DB/service call in `try/except`: domain exceptions → `HTTPException` with the appropriate status code (404, 400; `IdempotencyKeyConflictError`/`IdempotencyPayloadMismatchError` in `students/router.py` → 409/422); general `Exception` (e.g. Supabase `APIError`) → `HTTPException(500, detail=str(exc))`. No raw tracebacks ever reach the client.
- **Agent tools** wrap every DB/service call in `try/except Exception as exc: return {"error": err_msg(exc)}` (non-fatal; the LLM sees the error and can respond accordingly).

The service layer itself never catches — it raises and lets the caller decide the error shape. This keeps HTTP semantics out of the service layer and LLM error formatting out of tool implementations.

### Agent system

`app/features/agent/router.py` exposes one SSE endpoint:

**`POST /agent/chat`** — LangGraph multi-agent mode. Builds a supervisor + three subagents on every request and streams via `lg/stream_adapter.py`.

`state.py` — module-level `stop_signals: dict[str, bool]` keyed by `request_id`; set by `POST /agent/stop` to signal in-flight requests to stop.

See `claude/agent.md` for tool contract, system rules, SSE event types, and LangGraph graph detail.

### Shared utilities and Gemini

- `app/shared/utils.py` — `DAYS`, `TIME_SLOTS`, `time_to_mins`, `format_fee`, `get_weekday_dates`, and other date/weekday/time utilities shared across tools.
- `app/shared/gemini/client.py` — singleton `google.genai.Client` (`gemini_client`).
- `app/shared/gemini/slot_generation.py` — `run_gemini_slot_generation(prompt)` — calls `gemini-2.5-flash` with structured JSON output and validates via Pydantic.
- **LangGraph subagents:** `get_gemini_chat_model()` constructs a fresh `ChatGoogleGenerativeAI` per call — parallel subagents must not share a model instance.
