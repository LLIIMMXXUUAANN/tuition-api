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
  features/
    google/
      router.py        → Calendar/Drive CRUD + OAuth setup routes
      auth.py          → OAuth credentials, CSRF state tokens, token rotation
      calendar.py      → Recurring Calendar events (create / find / update)
      drive.py         → Student Drive folders + Meet docs
      cleanup.py       → Bulk delete on student removal
      sync.py          → Full sync for all active students
    students/
      router.py        → Student CRUD + portal lookup routes
      service.py       → StudentNotFoundError, create_student, update_student, delete_student
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
      router.py        → AI agent SSE — classic Gemini loop + LangGraph
      schema.py        → TOOL_DECLARATIONS (18 tools) + SYSTEM_INSTRUCTION
      eval.py          → self_eval — post-mutation DB verification (see Design decisions)
      state.py         → stop_signals dict (keyed by request_id)
      tools/
        shared.py        → err_msg helper, SupabaseClient type alias
        student_tools.py → 10 student + Google + portal tools
        template_tools.py → 3 template + payment-message tools
        timetable_tools.py → 5 timetable + slot-generation tools
      lg/
        model.py         → get_gemini_chat_model (fresh ChatGoogle per call)
        handoff.py       → create_dispatch_tool, normalize_agent_name
        subagent.py      → build_subagent (ReAct graph: agent → tools → post_hook → agent)
        tool_factories.py → make_student_tools / make_template_tools / make_timetable_tools (+ make_cannot_complete_tool appended to each)
        student_agent.py → make_student_agent
        template_agent.py → make_template_agent
        timetable_agent.py → make_timetable_agent
        supervisor.py    → make_supervisor, build_custom_supervisor
        post_hooks.py    → make_student_post_hook, make_timetable_post_hook
        stream_adapter.py → pipe_langgraph_stream, is_routing_relevant
  shared/
    db.py              → Async Supabase singleton (get_supabase)
    utils.py           → DAYS, TIME_SLOTS, time_to_mins, format_fee, get_weekday_dates…
    schema.py          → CamelResponse shared response class
    gemini/
      client.py        → Singleton google.genai.Client
      slot_generation.py → run_gemini_slot_generation (structured JSON output)
```

### Service layer pattern

Each feature has a `service.py` that owns domain logic and raises typed exceptions:

- **Domain exceptions** (e.g. `StudentNotFoundError`, `TimetableValidationError`) are raised by the service.
- **HTTP routers** catch domain exceptions → re-raise as `HTTPException` with the appropriate status code.
- **Agent tools** catch domain exceptions → return `{"error": str(err)}` (non-fatal; the LLM sees the error and can respond accordingly).

This keeps HTTP semantics out of the service layer and error handling out of tool implementations.

## Design decisions

### Self-evaluation after mutations (`eval.py`)

After any agent tool round that includes a write operation, `self_eval` runs a read-back query against Supabase to verify the mutation landed:

- `create_student` / `update_student` — SELECT by id, confirm row exists
- `delete_student` — SELECT by id, confirm row is gone
- `update_timetable_rules` — read back `timetable_rules`, compare to what was written
- `update_buffer_mins` — read back `timetable_buffer_mins`, compare parsed integer

**Per-round, not post-loop.** Both the classic Gemini loop and the LangGraph post-hooks run `self_eval` inside the tool-execution loop — once per tool round, covering all mutations from that round in parallel via `asyncio.gather`. If the agent updates two students in one round (the system prompt encourages batching), both are verified simultaneously, not just the last one.

**Passive audit (Option A) — results are shown to the user, never fed back to the agent.** A successful verification appears as a `✓ verified in DB` step in the chat UI; a failure appears as `⚠ could not verify`. The agent never sees these verdicts and cannot retry based on them.

This is the standard industry approach for interactive agents: transient infrastructure failures (a Supabase read racing against a just-completed write, a momentary network blip) should not trigger agent retries that risk duplicate writes. The human operator sees the audit result and can take corrective action if needed. Feeding verification failures back into the LLM loop treats a monitoring concern as an agent-control concern, which conflates two responsibilities and introduces the risk of write amplification.

### LangGraph dispatch reliability

Two structural invariants are enforced in code rather than relying solely on prompt rules:

**Same-agent dedup.** The supervisor LLM is prompted to combine tasks for the same subagent into one `dispatch` entry (so the subagent can batch tool calls internally). When the LLM creates two separate entries for the same agent anyway, `supervisor_node` merges them: after normalising `handoff_list`, a `merged: dict[str, str]` groups entries by `agentName` and joins tasks with `\n`. Only then are `Send` commands emitted — guaranteeing one subagent invocation per agent regardless of LLM compliance.

**UUID propagation.** `build_supervisor_prompt` instructs the supervisor to scan prior replies for `[student_id:NAME:UUID]` tokens and embed known UUIDs in task descriptions (e.g. `"Update Ang (id: 2dfa867c-...) fee to 60"`). `STUDENT_PROMPT` instructs the student_agent: if the task contains a UUID in parentheses, call `update_student` directly — no `search_students` needed. The student_agent also appends `[student_id:NAME:UUID]` tokens to every reply involving `get_student`, `create_student`, or `update_student`; these tokens flow into `lgHistory` via `is_routing_relevant`, making UUIDs available to the supervisor for subsequent turns.

### Supervisor silent relay guarantee (`supervisor.py`)

When a subagent completes its task and hands back to the supervisor, the supervisor must relay the subagent's reply verbatim. Three complementary layers enforce this:

**`content=""` on handoff AIMessage.** `_create_handoff_back_messages` sets the AIMessage content to an empty string. The prior value (`"Transferring back to supervisor"`) was a model-role message that Gemini mistook for its own prior output — causing it to consider its turn "already done" and produce no new text.

**Code fallback in `supervisor_node`.** After the LLM response is accumulated, if there is no `dispatch` call and no extracted text, `supervisor_node` scans backward through state messages for the most recent `transfer_back_to_*` ToolMessages (the subagent reply carriers) and joins their content as the reply. This deterministic relay path guarantees output even when the LLM goes silent — prompts define desired behaviour, code enforces invariants.

**CRITICAL prompt rule.** `build_supervisor_prompt` ends with an explicit rule: every supervisor turn must produce either a `dispatch` call or non-empty text; empty output is never valid. The rule also instructs the LLM to output the last ToolMessage content verbatim as a fallback.

The code fallback is the authoritative path (it never fails); the prompt rule reduces the frequency of the fallback being needed.

### Subagent `cannot_complete` tool (`tool_factories.py`)

Each subagent has a `cannot_complete(reason: str)` tool added via `make_cannot_complete_tool()`. When a subagent receives a task that doesn't match its available tools, it calls this instead of outputting vague free text.

- **Visible in LangSmith traces** as an explicit tool call step rather than an invisible free-text failure.
- **Structured signal** — the `reason` string is returned to the supervisor as the ToolMessage content, giving it a clear `"Cannot complete: ..."` message to relay to the user.
- **Forces articulation** — the subagent must state a specific reason rather than guessing or hallucinating a response.

The tool itself is trivial (`return f"Cannot complete: {reason}"`); the value is in directing the LLM to call it explicitly. No changes to `make_call_agent` or `supervisor_node` are needed — the existing ReAct loop handles it naturally (subagent calls tool → gets result → outputs final text → END → supervisor receives the reply via the handoff messages).

### UI side-effect events (`execute_tool` + `side_effects` list)

Two tools (`generate_slot_availability`, `download_timetable_image`) trigger frontend UI events — a download button appears in the chat after they run. These are emitted as SSE events of type `slots_ready` and `download_schedule`.

The coupling between "which tools trigger UI events" and the SSE emission is kept in `execute_tool`'s match block (the dispatch layer), not in the main generator loop. `execute_tool` accepts an optional `side_effects: list[dict] | None` parameter; the two special match cases append their event dict to the list if the result contains the expected key. The main loop creates the list, passes it to `run_tool`, and drains it with `yield` after all tools complete.

This mirrors the LangGraph version's `config.writer` callback pattern (`tool_factories.py` calls `writer(...)` from inside the tool wrapper; the stream adapter forwards `custom` events as SSE). Both approaches avoid tool-name checks in the main streaming loop — adding a new UI-trigger tool only requires editing `execute_tool`, not the loop.

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
