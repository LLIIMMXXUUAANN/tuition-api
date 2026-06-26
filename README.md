# tuition-api

FastAPI backend for the tuition management system. Handles Google Calendar/Drive integration, AI agent chat (LangGraph multi-agent), timetable slot generation, and payment message templating.

## Documentation

| File | Covers |
|---|---|
| `README.md` | Setup, env vars, OAuth, commands, API routes |
| `docs/decisions.md` | Non-obvious design decisions and the reasoning behind them |
| `CLAUDE.md` | AI assistant guidance — architecture, DB schema, service layer |
| `claude/agent.md` | Tool contract, system rules, SSE contract, LangGraph graph |
| `claude/timetable.md` | Timetable routes, slot classification algorithm |
| `claude/google.md` | Google OAuth, Calendar, Drive, cleanup, sync implementation |
| `docs/agent-tools.md` | Full 18-tool reference (input / process / output for each tool) |

All frontend documentation (features, UI, routing, components, decisions) is in the frontend repo — see `../Tuition/README.md` and `../Tuition/CLAUDE.md`.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)

## Quick start

```bash
uv sync
cp .env.example .env   # fill in values
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
| `LANGSMITH_TRACING` | `true` to enable LangSmith tracing (optional) |
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
3. Add `http://localhost:3000/api/google/callback` (and your production URL) to Authorised redirect URIs in Google Cloud Console
4. Set `GOOGLE_REDIRECT_URI` in `.env` to the Next.js callback URL
5. Visit `http://localhost:3000/api/google/auth` as admin — Next.js fetches the OAuth URL from FastAPI and redirects the browser; after consent, Google redirects to Next.js `/api/google/callback` which saves the refresh token via FastAPI

**Avoid 7-day token expiry:** Google expires refresh tokens every 7 days for apps in Testing mode. Publish the app to **In production** in Google Cloud Console → APIs & Services → OAuth consent screen → Publish App. No verification is needed for a single-user app.

If you see `invalid_grant` errors, re-visit `/api/google/auth` to re-authorize.

## Keeping the Render instance warm (free tier)

Render's free tier spins down after 15 minutes of inactivity, causing 50+ second cold starts. A cron job on [cron-job.org](https://cron-job.org) (free) pings the health endpoint every 10 minutes to prevent this.

**Setup:**
1. Sign up at cron-job.org
2. Create a new cron job:
   - **URL**: `https://tuition-api-uqq4.onrender.com/health`
   - **Schedule**: every 10 minutes
   - **Method**: GET
3. Enable it

The `/health` endpoint requires no auth so the ping always succeeds.

**When to skip this:** upgrade to Render Starter ($7/month) for a persistent instance that never sleeps.

---

## Email (magic link delivery)

Magic link emails are sent via Gmail SMTP. Configure in Supabase Dashboard → Authentication → SMTP Settings:

| Field | Value |
|---|---|
| Host | `smtp.gmail.com` |
| Port | `587` |
| Sender | `limxuan520@gmail.com` |
| Password | Gmail App Password (not the account password) |

To regenerate: Google Account → Security → search "App Passwords".

## API routes

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` or `/health` | — | Health check |
| `GET` | `/google/auth-url` | ✓ | Return Google OAuth consent URL |
| `POST` | `/google/callback` | ✓ | Exchange OAuth code for refresh token and save it |
| `POST` | `/google/create-class-event` | ✓ | Create weekly recurring Calendar events |
| `POST` | `/google/create-student-folder` | ✓ | Create Drive folder structure |
| `POST` | `/google/update-class-event` | ✓ | Nuke-and-repave Calendar events + update Drive doc |
| `POST` | `/google/delete-student` | ✓ | Trash Drive folder + delete Calendar events |
| `POST` | `/google/sync-all` | ✓ | Sync all active students' Google resources |
| `GET` | `/students` | ✓ | List students (optional `?status=Active`) |
| `GET` | `/students/portal-lookup` | ✓ | Find student by portal email |
| `GET` | `/students/{id}` | ✓ | Get single student |
| `POST` | `/students` | ✓ | Create student |
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
| `POST` | `/agent/chat` | ✓ | LangGraph multi-agent SSE stream |
| `POST` | `/agent/stop` | ✓ | Abort an in-flight agent request |
| `GET` | `/agent/conversations/current` | ✓ | Return (or create) the single latest conversation + its messages |
| `GET` | `/agent/conversations/{id}/messages` | ✓ | Fetch messages for a known conversation ID |
| `POST` | `/agent/conversations/{id}/clear` | ✓ | Delete all messages + reset LLM history columns |

✓ = requires `X-Internal-Secret` header
