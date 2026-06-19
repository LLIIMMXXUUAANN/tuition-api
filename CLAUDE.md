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

`app/main.py` mounts six routers from `app/features/`:

| Prefix | Router | Purpose |
|---|---|---|
| `/google` | `app/features/google/router.py` | Google Calendar/Drive CRUD (OAuth2) |
| `/students` | `app/features/students/router.py` | Student CRUD + portal lookup |
| `/payment` | `app/features/payment/router.py` | Payment message generation |
| `/timetable` | `app/features/timetable/router.py` | Timetable rules and slot generation |
| `/agent` | `app/features/agent/router.py` | AI agent SSE streaming (two modes) |
| `/templates` | `app/features/templates/router.py` | Message template read/update |

All routers require the `X-Internal-Secret` header (checked by `app/auth.py`). The OAuth flow uses Next.js as the browser-facing layer: `GET /google/auth-url` returns the consent URL (Next.js redirects the browser), and `POST /google/callback` receives `{code, state}` from Next.js after Google redirects back — both are protected, not browser-accessible.

### Configuration

`app/config.py` — a single `Settings` Pydantic model loaded from `.env`. All settings are accessed via the singleton `settings` imported from this module. Required env vars: `internal_api_secret`, `supabase_url`, `supabase_service_role_key`, `gemini_api_key`, `google_client_id`, `google_client_secret`, `google_redirect_uri`, `google_students_folder_id`, `google_calendar_id`.

### Supabase

`app/shared/db.py` — lazy singleton `AsyncClient`. Always obtained with `await get_supabase()`. Uses the service-role key (bypasses RLS). Google OAuth refresh token is stored in the `settings` Supabase table under key `google_refresh_token`.

### Shared types

`app/types.py` — the canonical `ClassSlot` and `Student` Pydantic models used across routers and tools.

### Service layer pattern

Each feature has a `service.py` that owns domain logic and raises typed exceptions:

- **Domain exceptions** (e.g. `StudentNotFoundError`, `TimetableValidationError`) are raised by the service.
- **HTTP routers** catch domain exceptions → re-raise as `HTTPException` with the appropriate status code.
- **Agent tools** catch domain exceptions → return `{"error": str(err)}` (non-fatal; the LLM sees the error and can respond accordingly).

This keeps HTTP semantics out of the service layer and prevents error handling from being duplicated across tools and routes.

### Agent system (two modes)

`app/features/agent/router.py` exposes two SSE endpoints:

1. **`POST /agent/chat`** — Classic single-agent mode. Drives the Google Gemini SDK (`google-genai`) directly in a tool-call loop (up to 10 rounds). Tool declarations live in `app/features/agent/schema.py`. Tool implementations are in `app/features/agent/tools/` and dispatched via a `match` block in `execute_tool`. `execute_tool` accepts an optional `side_effects: list[dict]` parameter — tools that trigger frontend UI events (download buttons) append their SSE event dict to this list; the main loop drains it with `yield` after all tools complete. After each tool round, all mutations are verified in parallel via `self_eval` (`app/features/agent/eval.py`) — see Design decisions below.

2. **`POST /agent/lg/chat`** — LangGraph multi-agent mode. Builds a supervisor + three subagents on every request (`make_supervisor` in `app/features/agent/lg/supervisor.py`) then streams via `app/features/agent/lg/stream_adapter.py`.

`app/features/agent/state.py` — module-level `stop_signals: dict[str, bool]` keyed by `request_id`; set by `POST /agent/stop` to signal in-flight requests to stop between tool rounds.

Both endpoints emit the same SSE event types: `chunk`, `step`, `done`, `stopped`, `error`, `ui_action`, and `history`/`lg_history`. UI-trigger tools (`generate_slot_availability`, `download_timetable_image`) emit `{"type": "ui_action", "action": "<name>", "payload": {...}}` — a generic envelope so new UI-trigger tools only need a new `action` value, not a new event type. A `requestId` can be used with `POST /agent/stop` to abort in-flight requests (via `app/features/agent/state.stop_signals` dict).

### LangGraph multi-agent graph

```
START → supervisor ──dispatch──► student_agent  ──► supervisor → END
                              ├─► template_agent ──► supervisor
                              └─► timetable_agent──► supervisor
```

- **`app/features/agent/lg/supervisor.py`** — `make_supervisor(supabase, date_string)` + `build_custom_supervisor()`: custom supervisor that avoids `@langchain/langgraph-supervisor`; fixes echoing issues; routes with a single `dispatch` tool (`app/features/agent/lg/handoff.py`) containing `handoffs: [{agentName, task}]`. Parallel dispatch is a single `Command` with multiple `Send` targets. One LLM call per supervisor turn (not two). **Same-agent dedup:** after parsing `handoff_list`, `supervisor_node` groups entries by `agentName` in a `merged` dict — multiple entries for the same agent are joined with `\n` into one combined task before `Send` commands are emitted, enforcing the "one entry per agent" invariant in code regardless of LLM compliance. **UUID propagation:** `build_supervisor_prompt` instructs the supervisor to scan prior replies for `[student_id:NAME:UUID]` tokens and embed known UUIDs directly in task descriptions (e.g. "Update Ang (id: 2dfa867c-...) fee to 60") so the student_agent can call `update_student` without a redundant `search_students` round. **Silent relay guarantee:** three layers ensure the supervisor always outputs the subagent reply — `content=""` on the handoff AIMessage (removes confusing model-role text that caused Gemini to go silent), a code fallback in `supervisor_node` that deterministically relays the last `transfer_back_to_*` ToolMessages when the LLM goes silent, and a CRITICAL prompt rule forbidding empty output.
- **`app/features/agent/lg/subagent.py`** — `build_subagent()` creates a standard ReAct graph. `TERMINAL_TOOLS = {"final_answer", "cannot_complete"}`. After `tools`/`post_hook`, `route_after_tools` checks the last AIMessage's tool_calls — if any name is in `TERMINAL_TOOLS`, routes to `END` (skipping the next agent LLM call); otherwise routes back to `agent`. `should_continue` on the `agent` node remains as a non-compliance safety net (no tool calls at all → `END` without entering `ToolNode`).
- **`app/features/agent/lg/tool_factories.py`** — `make_student_tools()`, `make_template_tools()`, `make_timetable_tools()`: wrap the 18 shared tool implementations in Pydantic schemas for LangGraph. Each factory appends `make_cannot_complete_tool()` (structured failure signal) and `make_final_answer_tool()` (structured success exit — `final_answer(text)` returns `text` unchanged; the ToolMessage content becomes the subagent's reply, saving one LLM summarization call per invocation).
- Each of the three subagents (`student_agent.py`, `template_agent.py`, `timetable_agent.py`) wraps its tool set with a domain-specific system prompt. `student_agent` is instructed: if the task explicitly contains a UUID in parentheses, use it directly and skip `search_students`; always append `[student_id:NAME:UUID]` tokens at the end of any reply involving `get_student`, `create_student`, or `update_student` (these tokens propagate UUIDs back into `lgHistory` for the supervisor to reuse next turn). All three subagents include prompt rules directing them to call `cannot_complete(reason=...)` on tool mismatch and `final_answer(text=...)` as the mandatory ending for every successful reply. `make_call_agent` in `supervisor.py` now extracts the reply from the `final_answer`/`cannot_complete` ToolMessage first, falling back to a free-text AIMessage.
- **`app/features/agent/lg/agent_state.py`** — `AgentState`: `messages: Annotated[list[BaseMessage], add_messages]` + `audit_log: Annotated[list[str], operator.add]`. All subagents and the supervisor use this instead of the built-in `MessagesState`. `audit_log` accumulates self-eval verdicts across tool rounds and is never sent to the LLM.
- **`app/features/agent/lg/post_hooks.py`** — `make_student_post_hook()`, `make_timetable_post_hook()`: run `self_eval` for mutations in the current tool round (in parallel via `asyncio.gather`) and return `{"audit_log": [combined_verdict]}`. `_find_round_mutation_calls` scans backward for the last AIMessage with tool_calls (current round only).
- **`app/features/agent/lg/stream_adapter.py`** — `pipe_langgraph_stream()` translates LangGraph's `(namespace, mode, data)` event tuples into the same SSE event types as the classic endpoint. In `updates` mode, drains `audit_log` from each node's output and emits verdicts as SSE `step` events. `is_routing_relevant()` filters what gets stored in `lgHistory` (keeps human messages, dispatch decisions, subagent final replies; drops subagent-internal tool calls and all SystemMessages).

### Google services

All Google API calls go through `app/features/google/`:

- **`auth.py`** — `get_oauth2_credentials(supabase)` reads the refresh token from the `settings` table and returns `(Credentials, original_token)`. `save_token_if_rotated(creds, original_token, supabase)` detects and persists rotated refresh tokens (Google Auth updates `creds.refresh_token` in-place on rotation). CSRF protection: `generate_state_token()` / `verify_and_consume_state(token)` use a module-level `_pending_states: dict[str, float]` with a 10-minute TTL — the state token is embedded in the OAuth redirect URL and verified before the code exchange. Every endpoint that calls Google APIs unpacks the tuple and calls `save_token_if_rotated` after the operation.

- **`calendar.py`** — `create_weekly_class_events` creates recurring events; first slot gets a Google Meet conference. `update_weekly_class_events` is nuke-and-repave: patches the primary event (the one with `hangoutLink`) or creates a new one with `conferenceData` if the primary was deleted; always returns `effective_meet_link` (existing `hangoutLink` from primary, or newly generated link) alongside the backward-compat `meet_link` (only set when freshly generated). `find_recurring_event_ids` searches Calendar over 90 days. `meet_link` param is `str | None = None` — callers may omit it when the DB has no stored link.

- **`drive.py`** — student Drive folder creation (`My Python Syllabus`: 4 subfolders + Meet doc; `Other Syllabus`: Meet doc only) and `update_student_meet_doc` rewrites the "Google Meet Link" doc.

- **`cleanup.py`** — `delete_student_google` trashes Drive folder + deletes Calendar events in parallel via `asyncio.gather`; both operations are non-fatal. Calendar deletes collect per-event failures and raise `RuntimeError` if any event could not be deleted (404/410 are silently swallowed as already-deleted).

- **`sync.py`** — `sync_all_students` handles all missing-resource combinations. Only skips students with no `class_schedule`. For each student: (1) search Calendar and merge found IDs with DB IDs; (2) if event IDs exist → `update_weekly_class_events` (nuke-and-repave, recovers existing Meet link via `effective_meet_link`); if none → `create_weekly_class_events` (fresh creation); (3) save updated IDs + Meet link to DB only if changed; (4) if Drive folder missing → `create_student_drive_folder`; if folder exists → `update_student_meet_doc`. `invalid_grant` errors surface as "Google auth expired — reconnect".

### Shared utilities

`app/shared/` contains cross-feature code with no feature-specific logic:
- `db.py` — `get_supabase()` async Supabase singleton
- `utils.py` — `DAYS`, `TIME_SLOTS`, `time_to_mins`, `format_fee`, `get_weekday_dates`, and other date/weekday/time utilities shared across tools
- `schema.py` — `CamelResponse` shared response class
- `gemini/client.py` — singleton `google.genai.Client` (`gemini_client`)
- `gemini/slot_generation.py` — `run_gemini_slot_generation(prompt)` — calls `gemini-2.5-flash` with structured JSON output and validates via Pydantic

### Gemini integration

- **Classic agent:** `google-genai` SDK — `app/shared/gemini/client.py` exports a module-level singleton `gemini_client` (`google.genai.Client`). `generate_content_stream` returns a coroutine in the current SDK version — always `await` it before `async for`: `async for chunk in await gemini_client.aio.models.generate_content_stream(...)`.
- **LangGraph subagents:** `langchain-google-genai` — `app/features/agent/lg/model.py` exports `get_gemini_chat_model()` which returns a fresh `ChatGoogleGenerativeAI` instance per call (model: `gemini-2.5-flash`, `temperature=0`, `thinking_budget=0` to disable the thinking pass). Parallel subagents must not share a model instance — hence `get_gemini_chat_model()` constructs a new one each time.
- **LangSmith tracing:** opt-in via `langsmith_tracing=true` in `.env`.

## Design decisions

### Self-evaluation after mutations (`eval.py`)

After any agent tool round that includes a write operation, `self_eval` runs a read-back query against Supabase to verify the mutation landed:

- `create_student` / `update_student` — SELECT by id, confirm row exists
- `delete_student` — SELECT by id, confirm row is gone
- `update_timetable_rules` — read back `timetable_rules` from `settings`, compare to what was written
- `update_buffer_mins` — read back `timetable_buffer_mins`, compare parsed integer

**Per-round, not post-loop.** Both the classic Gemini loop (in `router.py`) and the LangGraph post-hooks (in `post_hooks.py`) run `self_eval` inside the tool-execution loop — once per tool round, covering all mutations from that round in parallel via `asyncio.gather`. If the agent updates two students in one round (the system prompt encourages batching — Rule 12), both are verified simultaneously, not just the last one.

`_find_round_mutation_calls` (LangGraph post-hook helper) searches backward from the end of state messages, stopping at the most recent `SystemMessage(name="self_eval")` to avoid re-examining mutations from prior rounds.

**Passive audit (Option A) — results shown to the user, never fed back to the agent.** A successful verification appears as a `✓ verified in DB` step in the chat UI; a failure appears as `⚠ could not verify`. The agent never sees these verdicts and cannot retry based on them.

This is the standard industry approach for interactive agents: transient infrastructure failures (a Supabase read racing against a just-completed write, a momentary network blip) should not trigger agent retries that risk duplicate writes. The human operator sees the audit result and can take corrective action if needed. Feeding verification failures back into the LLM loop (Option B) treats a monitoring concern as an agent-control concern, conflates two responsibilities, and introduces the risk of write amplification.

### Supervisor silent relay guarantee (`supervisor.py`)

When a subagent completes and hands back to the supervisor, the supervisor must relay the reply verbatim. Three complementary layers enforce this:

**`content=""` on handoff AIMessage.** `_create_handoff_back_messages` sets the AIMessage content to `""`. The prior value (`"Transferring back to supervisor"`) was a model-role message that Gemini mistook for its own prior output, causing it to consider its turn "already done" and produce no new text.

**Code fallback in `supervisor_node`.** If the LLM produces no `dispatch` call and no text, `supervisor_node` scans backward through state messages for the most recent `transfer_back_to_*` ToolMessages and joins their content as the reply. Prompts define desired behaviour; code enforces invariants.

**CRITICAL prompt rule.** `build_supervisor_prompt` ends with: every supervisor turn must produce either a `dispatch` call or non-empty text; empty output is never valid. The code fallback is authoritative (never fails); the prompt rule reduces how often it's needed.

### Subagent terminal tools (`tool_factories.py` + `subagent.py`)

Each subagent has two terminal tools that end its turn without an extra LLM summarization call:

**`final_answer(text)`** — normal exit. The subagent calls this with its complete reply (including tables, tokens, formatted content). `route_after_tools` in `subagent.py` detects it and routes to `END` immediately. The ToolMessage content becomes the reply in `make_call_agent`.

**`cannot_complete(reason)`** — failure exit. Called when the task doesn't match available tools. Visible in LangSmith as an explicit tool step; surfaces a clear `"Cannot complete: ..."` reason to the supervisor.

`should_continue` on the `agent` node is retained as a non-compliance fallback: if the LLM outputs no tool calls at all, it routes to `END` without entering `ToolNode` (which would crash on empty tool calls). The supervisor does NOT get these tools — it is a single-turn routing node, not a ReAct loop, so terminal tools provide no benefit there.
