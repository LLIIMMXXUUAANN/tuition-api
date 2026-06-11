# tuition-api

FastAPI backend for the tuition management system. Handles Google Calendar/Drive integration, AI agent chat (classic Gemini loop + LangGraph multi-agent), timetable slot generation, and payment message templating.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)

## Quick start

```bash
# Install dependencies
uv sync

# Copy env and fill in values
cp .env.example .env

# Run dev server (auto-reload)
uv run uvicorn app.main:app --reload
```

Server starts at `http://127.0.0.1:8000`. API docs at `http://127.0.0.1:8000/docs`.

## Environment variables

| Variable | Description |
|---|---|
| `INTERNAL_API_SECRET` | Shared secret with the Next.js frontend — all protected requests must include `X-Internal-Secret: <value>` |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Service-role key (bypasses RLS — server-side only) |
| `GEMINI_API_KEY` | Google AI Studio API key (Gemini 2.5 Flash) |
| `GOOGLE_CLIENT_ID` | OAuth 2.0 client ID |
| `GOOGLE_CLIENT_SECRET` | OAuth 2.0 client secret |
| `GOOGLE_REDIRECT_URI` | OAuth callback URI — must match Google Cloud console (`http://localhost:3000/api/google/callback` locally) |
| `GOOGLE_STUDENTS_FOLDER_ID` | Drive folder ID where student folders are created |
| `GOOGLE_CALENDAR_ID` | Google Calendar ID for class events |
| `GOOGLE_LEC_TOPIC1_FILE_ID` | Drive file ID for Topic 1 shortcut (My Python Syllabus students only) |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins (e.g. `http://localhost:3000`) |
| `LANGCHAIN_TRACING` | `true` to enable LangSmith tracing (optional) |
| `LANGSMITH_ENDPOINT` | LangSmith API endpoint |
| `LANGSMITH_API_KEY` | LangSmith API key |
| `LANGSMITH_PROJECT` | LangSmith project name (default: `tuition-agent`) |

See `.env.example` for a template with comments.

## Dev commands

```bash
uv run uvicorn app.main:app --reload   # dev server
uv run pytest                          # run all tests
uv run pytest tests/test_agent.py -v  # single test file
uv run ruff check .                   # lint
uv run ruff format .                  # format
uv run pyright                        # type-check
```

Tests hit real Supabase — set up `.env` before running.

## One-time Google OAuth setup

1. Enable Drive API + Calendar API in Google Cloud Console
2. Add the admin email as a test user on the OAuth consent screen
3. In Google Cloud Console → OAuth credentials → Authorised redirect URIs, add `http://localhost:3000/api/google/callback` (local) and your production URL (e.g. `https://lim-tuition.vercel.app/api/google/callback`)
4. Set `GOOGLE_REDIRECT_URI` in `tuition-api/.env` to the Next.js callback URL (e.g. `http://localhost:3000/api/google/callback`)
5. Visit `http://localhost:3000/api/google/auth` as admin — Next.js fetches the OAuth URL from FastAPI and redirects the browser; after consent, Google redirects to Next.js `/api/google/callback` which saves the refresh token via FastAPI

## Architecture

```
app/
  main.py              → FastAPI app, CORS, router mounts
  config.py            → Pydantic Settings (loaded from .env)
  auth.py              → require_internal_secret dependency
  types.py             → ClassSlot, Student, enums (StudentMode, StudentStatus, SlotState…)
  routers/
    google.py          → Calendar/Drive CRUD + OAuth setup (all routes protected)
    students.py        → Student CRUD + portal lookup
    payment.py         → Payment message generation
    timetable.py       → Timetable rules + buffer + slot generation
    agent.py           → AI agent SSE — classic Gemini loop + LangGraph
    templates.py       → Message template read/update
  services/
    supabase_client.py → Async Supabase singleton (get_supabase)
    google/
      auth.py          → OAuth credentials, CSRF state tokens, token rotation
      calendar.py      → Recurring Calendar events (create / find / update)
      drive.py         → Student Drive folders + Meet docs
      cleanup.py       → Bulk delete on student removal
      sync.py          → Full sync for all active students
    gemini/
      client.py        → Singleton google.genai.Client
      slot_generation.py → run_gemini_slot_generation (structured JSON output)
  agent/
    schema.py          → TOOL_DECLARATIONS (19 tools) + SYSTEM_INSTRUCTION
    eval.py            → self_eval — post-mutation DB verification
    state.py           → stop_signals dict (keyed by request_id)
    tools/
      shared.py        → err_msg helper, SupabaseClient type alias
      student_tools.py → 11 student + Google + portal tools
      template_tools.py → 3 template + payment-message tools
      timetable_tools.py → 5 timetable + slot-generation tools
    lg/
      model.py         → get_gemini_chat_model (fresh ChatGoogle per call)
      handoff.py       → HandoffTask, create_dispatch_tool
      subagent.py      → build_subagent (ReAct graph: agent → tools → post_hook → agent)
      tool_factories.py → make_student_tools / make_template_tools / make_timetable_tools
      student_agent.py → make_student_agent
      template_agent.py → make_template_agent
      timetable_agent.py → make_timetable_agent
      supervisor.py    → make_supervisor, build_custom_supervisor
      post_hooks.py    → make_student_post_hook, make_timetable_post_hook
      stream_adapter.py → pipe_langgraph_stream, is_routing_relevant
  lib/
    utils.py           → DAYS, TIME_SLOTS, time_to_mins, format_fee, get_weekday_dates…
    templates.py       → TEMPLATE_META, template_meta()
    payment.py         → PaymentStudentData, build_payment_message()
    timetable_slots.py → compute_buffer_slots, build_booked_cell_set, run_slot_generation
```

## API routes

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` or `/health` | — | Health check |
| `GET` | `/google/auth-url` | ✓ | Return Google OAuth consent URL (Next.js fetches this, then redirects the browser) |
| `POST` | `/google/callback` | ✓ | Exchange OAuth code for refresh token and save it (called by Next.js after Google redirects) |
| `POST` | `/google/create-class-event` | ✓ | Create weekly recurring Calendar events |
| `POST` | `/google/create-student-folder` | ✓ | Create Drive folder structure |
| `POST` | `/google/update-class-event` | ✓ | Nuke-and-repave Calendar events + update Drive doc |
| `POST` | `/google/delete-student` | ✓ | Trash Drive folder + delete Calendar events |
| `POST` | `/google/sync-all` | ✓ | Sync all active students' Google resources |
| `GET` | `/students` | ✓ | List students (optional `?status=Active`) |
| `GET` | `/students/portal-lookup` | ✓ | Find student by portal email |
| `GET` | `/students/{id}` | ✓ | Get single student |
| `POST` | `/students` | ✓ | Create student (auto Google setup if schedule provided) |
| `PUT` | `/students/{id}` | ✓ | Update student (auto Calendar/Drive sync on schedule change) |
| `DELETE` | `/students/{id}` | ✓ | Delete student + Google cleanup |
| `POST` | `/payment/generate` | ✓ | Generate payment reminder message |
| `GET` | `/timetable/rules` | ✓ | Get scheduling rules |
| `POST` | `/timetable/rules` | ✓ | Update scheduling rules |
| `GET` | `/timetable/buffer-mins` | ✓ | Get buffer minutes |
| `POST` | `/timetable/buffer-mins` | ✓ | Update buffer minutes (0–60) |
| `POST` | `/timetable/generate-slots` | ✓ | AI slot availability classification |
| `GET` | `/templates` | ✓ | List all message templates |
| `PUT` | `/templates/{id}` | ✓ | Update template content |
| `POST` | `/agent/chat` | ✓ | Classic Gemini agent SSE stream |
| `POST` | `/agent/lg/chat` | ✓ | LangGraph multi-agent SSE stream |
| `POST` | `/agent/stop` | ✓ | Abort an in-flight agent request |

✓ = requires `X-Internal-Secret` header
