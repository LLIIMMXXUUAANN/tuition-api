# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the dev server (reload on change)
uv run uvicorn app.main:app --reload

# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_agent.py -v

# Run a single test by name
uv run pytest tests/test_students.py::test_create_student -v

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type-check
uv run pyright
```

Tests require a valid `.env` (same settings as the server — they spin up a real `TestClient` against the app and hit real Supabase).

## Architecture

This is a FastAPI backend for a tuition management system. Every module is a port of a TypeScript/Next.js backend (`src/features/...`), so the module docstrings always reference their original file.

### Entry point and routing

`app/main.py` mounts five routers:

| Prefix | Router | Purpose |
|---|---|---|
| `/google` | `app/routers/google.py` | Google Calendar/Drive CRUD (OAuth2) |
| `/payment` | `app/routers/payment.py` | Payment record endpoints |
| `/timetable` | `app/routers/timetable.py` | Timetable rules and slot generation |
| `/agent` | `app/routers/agent.py` | AI agent SSE streaming (two modes) |
| _(no prefix)_ | `app/routers/students.py` | Student CRUD (`/students/*`) |

All routers except the two OAuth endpoints require the `X-Internal-Secret` header (checked by `app/auth.py`). The Google router uses two APIRouter instances: `router` (protected) and `public_router` (no auth) — OAuth endpoints must be browser-accessible and cannot carry the internal secret header.

### Configuration

`app/config.py` — a single `Settings` Pydantic model loaded from `.env`. All settings are accessed via the singleton `settings` imported from this module. Required env vars: `internal_api_secret`, `supabase_url`, `supabase_service_role_key`, `gemini_api_key`, `google_client_id`, `google_client_secret`, `google_redirect_uri`, `google_students_folder_id`, `google_calendar_id`.

### Supabase

`app/services/supabase_client.py` — lazy singleton `AsyncClient`. Always obtained with `await get_supabase()`. Uses the service-role key (bypasses RLS). Google OAuth refresh token is stored in the `settings` Supabase table under key `google_refresh_token`.

### Shared types

`app/types.py` — the canonical `ClassSlot` and `Student` Pydantic models used across routers and tools.

### Agent system (two modes)

`app/routers/agent.py` exposes two SSE endpoints:

1. **`POST /agent/chat`** — Classic single-agent mode. Drives the Google Gemini SDK (`google-genai`) directly in a tool-call loop (up to 10 rounds). Tool declarations live in `app/agent/schema.py`. Tool implementations are in `app/agent/tools/` and dispatched via a `match` block in `execute_tool`. Mutations trigger a post-call self-eval (`app/agent/eval.py`).

2. **`POST /agent/lg/chat`** — LangGraph multi-agent mode. Builds a supervisor + three subagents on every request (`make_supervisor` in `app/agent/lg/supervisor.py`) then streams via `app/agent/lg/stream_adapter.py`.

Both endpoints emit the same SSE event types: `chunk`, `step`, `done`, `stopped`, `error`, `download_schedule`, `slots_ready`, and `history`/`lg_history`. A `requestId` can be used with `POST /agent/stop` to abort in-flight requests (via `app/agent/state.stop_signals` dict).

### LangGraph multi-agent graph

```
START → supervisor ──dispatch──► student_agent  ──► supervisor → END
                              ├─► template_agent ──► supervisor
                              └─► timetable_agent──► supervisor
```

- **`app/agent/lg/supervisor.py`** — custom supervisor that avoids `@langchain/langgraph-supervisor`; fixes echoing issues; routes with a single `dispatch` tool containing `handoffs: [{agentName, task}]`. Parallel dispatch is a single `Command` with multiple `Send` targets.
- **`app/agent/lg/subagent.py`** — `build_subagent()` creates a standard ReAct graph (agent → tools → optional post-hook → agent → END).
- Each of the three subagents (`student_agent.py`, `template_agent.py`, `timetable_agent.py`) wraps its tool set with a system prompt.
- **`app/agent/lg/stream_adapter.py`** — translates LangGraph's `(namespace, mode, data)` event tuples into the same SSE event types as the classic endpoint. `is_routing_relevant()` filters what gets stored in `lgHistory`.

### Google services

All Google API calls go through `app/services/google/`:

- **`auth.py`** — `get_oauth2_credentials(supabase)` reads the refresh token from the `settings` table and returns `(Credentials, original_token)`. `save_token_if_rotated(creds, original_token, supabase)` detects and persists rotated refresh tokens (Google Auth updates `creds.refresh_token` in-place on rotation). CSRF protection: `generate_state_token()` / `verify_and_consume_state(token)` use a module-level `_pending_states: dict[str, float]` with a 10-minute TTL — the state token is embedded in the OAuth redirect URL and verified before the code exchange. Every endpoint that calls Google APIs unpacks the tuple and calls `save_token_if_rotated` after the operation.

- **`calendar.py`** — `create_weekly_class_events` creates recurring events; first slot gets a Google Meet conference. `update_weekly_class_events` is nuke-and-repave: patches the primary event (the one with `hangoutLink`) or creates a new one with `conferenceData` if the primary was deleted; always returns `effective_meet_link` (existing `hangoutLink` from primary, or newly generated link) alongside the backward-compat `meet_link` (only set when freshly generated). `find_recurring_event_ids` searches Calendar over 90 days. `meet_link` param is `str | None = None` — callers may omit it when the DB has no stored link.

- **`drive.py`** — student Drive folder creation (`My Python Syllabus`: 4 subfolders + Meet doc; `Other Syllabus`: Meet doc only) and `update_student_meet_doc` rewrites the "Google Meet Link" doc.

- **`cleanup.py`** — `delete_student_google` trashes Drive folder + deletes Calendar events in parallel via `asyncio.gather`; both operations are non-fatal. Calendar deletes collect per-event failures and raise `RuntimeError` if any event could not be deleted (404/410 are silently swallowed as already-deleted).

- **`sync.py`** — `sync_all_students` handles all missing-resource combinations. Only skips students with no `class_schedule`. For each student: (1) search Calendar and merge found IDs with DB IDs; (2) if event IDs exist → `update_weekly_class_events` (nuke-and-repave, recovers existing Meet link via `effective_meet_link`); if none → `create_weekly_class_events` (fresh creation); (3) save updated IDs + Meet link to DB only if changed; (4) if Drive folder missing → `create_student_drive_folder`; if folder exists → `update_student_meet_doc`. `invalid_grant` errors surface as "Google auth expired — reconnect".

### Lib utilities

`app/lib/` contains pure business logic (no I/O):
- `timetable_slots.py` — slot availability algorithm
- `templates.py` — payment/review/recommendation message template definitions
- `payment.py` — fee calculation helpers
- `utils.py` — date, weekday, and time utilities shared across tools

### Gemini integration

- Classic agent: `google-genai` SDK (`genai.Client.aio.models.generate_content_stream`), model `gemini-2.5-flash`
- LangGraph subagents: `langchain-google-genai` via `app/agent/lg/model.py` (`get_gemini_chat_model`)
- LangSmith tracing: opt-in via `langchain_tracing=true` in `.env`
