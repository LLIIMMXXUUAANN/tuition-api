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
| `INTERNAL_API_SECRET` | Shared secret with the Next.js frontend (all requests must include `X-Internal-Secret: <value>`) |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Service-role key (bypasses RLS — server-side only) |
| `GEMINI_API_KEY` | Google AI Studio API key (Gemini 2.5 Flash) |
| `GOOGLE_CLIENT_ID` | OAuth 2.0 client ID |
| `GOOGLE_CLIENT_SECRET` | OAuth 2.0 client secret |
| `GOOGLE_REDIRECT_URI` | OAuth callback URI (must match Google Cloud console) |
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
3. Set `GOOGLE_REDIRECT_URI=http://127.0.0.1:8000/google/callback` (local) or your production URL
4. Visit `http://127.0.0.1:8000/google/auth` as admin — completes the OAuth flow and stores the refresh token in Supabase

## Architecture

```
app/
  main.py              → FastAPI app, CORS, router mounts
  config.py            → Pydantic Settings (loaded from .env)
  auth.py              → require_internal_secret dependency
  types.py             → ClassSlot, Student Pydantic models
  routers/
    google.py          → Calendar/Drive CRUD + OAuth setup
    students.py        → Student CRUD
    payment.py         → Payment record endpoints
    timetable.py       → Timetable rules + slot generation
    agent.py           → AI agent SSE (classic + LangGraph)
  services/
    supabase_client.py → Async Supabase singleton
    google/
      auth.py          → OAuth credentials + CSRF state tokens
      calendar.py      → Recurring Calendar events
      drive.py         → Student Drive folders + Meet docs
      cleanup.py       → Bulk delete on student removal
      sync.py          → Full sync for all active students
    gemini/            → Gemini slot generation
  agent/
    schema.py          → Tool declarations + system instruction
    eval.py            → Post-mutation self-eval
    tools/             → 19 tool implementations
    state.py           → Stop signals + abort controllers
    lg/                → LangGraph multi-agent graph
  lib/
    timetable_slots.py → Slot availability algorithm
    templates.py       → Message template definitions
    payment.py         → Fee calculation
    utils.py           → Date/time/weekday utilities
```

### API routes

| Method | Path | Description |
|---|---|---|
| `GET` | `/google/auth` | Redirect to Google OAuth consent screen |
| `GET` | `/google/callback` | OAuth callback — saves refresh token |
| `POST` | `/google/create-class-event` | Create weekly recurring Calendar events |
| `POST` | `/google/create-student-folder` | Create Drive folder structure |
| `POST` | `/google/update-class-event` | Nuke-and-repave Calendar events |
| `POST` | `/google/sync-all` | Sync all active students' Google resources |
| `POST` | `/google/delete-student` | Trash Drive folder + delete Calendar events |
| `GET/POST` | `/students` | List / create students |
| `GET/PATCH/DELETE` | `/students/{id}` | Get / update / delete student |
| `POST` | `/payment/generate` | Generate payment message from template |
| `GET/POST` | `/timetable/rules` | Read / update scheduling rules |
| `GET/POST` | `/timetable/buffer-mins` | Read / update buffer minutes |
| `POST` | `/timetable/generate-slots` | AI slot availability classification |
| `POST` | `/agent/chat` | Classic Gemini agent SSE stream |
| `POST` | `/agent/lg/chat` | LangGraph multi-agent SSE stream |
| `POST` | `/agent/stop` | Abort an in-flight agent request |
