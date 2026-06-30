## Agent tool contract

### Tool design philosophy

Fine-grained reads, coarse-grained writes. Read tools (`search_students`, `get_student`, `list_students`, `get_schedule`, `get_fee_summary`, `list_templates`, `get_template`, `get_timetable_settings`) are granular so the LLM picks exactly the data shape needed. Write tools (`sync_all_students`) are compound — they bundle steps the user always wants together to reduce round trips and planning burden on the LLM. Keep total tool count under ~20 to avoid description-space crowding that degrades tool-selection accuracy.

### Tools (all 18)

| Tool | Required | Optional | Returns |
|---|---|---|---|
| `search_students` | `query` | — | `{ students: [{ id, name, status, class_schedule }] }` |
| `get_student` | `id` | — | `{ student: <all fields> }` |
| `list_students` | — | `status` | `{ students: [{ id, name, status, mode, fee_per_hour, class_schedule }] }` |
| `create_student` | `name`, `mode`, `fee_per_hour` | all other fields | `{ id, name, google_warning?: string }` |
| `update_student` | `id`, `fields` | — | `{ success: true, googleWarnings?: string[] }` |
| `delete_student` | `id` | — | `{ success: true, warnings?: string[] }` |
| `sync_all_students` | — | — | `{ results: [...] }` |
| `manage_portal_access` | `student_id`, `action`, `email` | — | `{ result: string }` |
| `get_schedule` | `day` (Monday–Sunday) | — | `{ day, students: [{ id, name, slots: [{ start, end }] }] }` |
| `get_fee_summary` | — | `month`, `year` | `{ month, year, students: [{ id, name, fee }], total }` |
| `list_templates` | — | — | `{ templates: [{ id, title, description }] }` |
| `get_template` | `id` | — | `{ template: { id, title, description, content } }` |
| `generate_payment_message` | `student_id` | `month`, `year`, `template_type`, `carryover` | `{ message, month, year, monthName }` |
| `get_timetable_settings` | — | — | `{ rules: string, bufferMins: number }` |
| `update_timetable_rules` | `rules` | — | `{ ok: true }` or `{ error: string }` |
| `update_buffer_mins` | `buffer_mins` | — | `{ ok: true }` or `{ error: string }` |
| `generate_slot_availability` | — | `student_availability` | `{ slots: ClassifiedSlot[] }` or `{ error: string }` |
| `download_timetable_image` | — | — | `{ students: ScheduleStudent[] }` or `{ error: string }` |

### Tool implementation notes (`app/features/agent/tools/`)

- **Exception handling pattern:** all tool functions wrap every Supabase and service call in `try/except Exception as exc: return {"error": err_msg(exc)}`. This is industry practice for LLM tool functions — the LLM receives a clean structured `{"error": "..."}` dict rather than a raw exception propagating through LangGraph's `ToolNode`. The service layer (e.g. `students/service.py`) is intentionally left without try/except — it raises, and the tool layer catches. `list_templates` is the only exception: it is a pure in-memory function with no I/O.
- `ALLOWED_UPDATE_KEYS` set — allowlist of writable columns for `update_student`; prevents prompt injection from touching columns not in the set
- `update_student` auto-syncs Calendar + Drive when `class_schedule` is in the updated fields: if `calendar_event_ids` + `google_meet_link` are set, calls `update_weekly_class_events` (nuke-and-repave) and `update_student_meet_doc` in parallel via `asyncio.gather`; if a new Meet link is generated (primary was deleted), also saves it to DB and re-updates the Drive doc; Google failures are non-fatal (returned as `googleWarnings`)
- `create_student` inserts the record and returns `{ id, name }`; includes `google_warning` if the service layer surfaces a non-fatal Google note
- `delete_student` attempts Google cleanup (Drive trash + Calendar delete) before the DB delete; Google failure is non-fatal
- `err_msg(err, fallback)` — use everywhere instead of inlining error strings (`app/features/agent/tools/shared.py`)
- `get_fee_summary` uses `get_weekday_dates` (from `app/shared/utils.py`) for exact session counting; tracks raw fees in a parallel array to avoid per-student rounding accumulation before summing the total
- `list_templates` is a pure function — no DB call. All metadata (id, title, description) lives in the in-memory `TEMPLATE_META` from `app/features/templates/service.py`; only `get_template` hits the DB to fetch `content`
- `generate_payment_message` defaults to next calendar month (MYT) when `month`/`year` are omitted; delegates all calculation to `build_payment_message()` from `app/features/payment/service.py`; returns `{ message, month, year, monthName }`
- `get_timetable_settings` fetches `timetable_rules` and `timetable_buffer_mins` from `settings` in parallel; returns `{ rules, bufferMins }` (bufferMins defaults to 15 if unset)
- `update_timetable_rules` / `update_buffer_mins` upsert into `settings` table; `update_buffer_mins` validates 0–60 before writing
- `generate_slot_availability` fetches rules, buffer, and all active students' `class_schedule` in a single `asyncio.gather`; delegates to `run_gemini_slot_generation` from `app/shared/gemini/slot_generation.py`; returns `{ error }` if no rules are configured
- `download_timetable_image` fetches active students' `name` and `class_schedule` ordered by name; returns `{ students }` which the router emits as a `ui_action` SSE event

---

## Persistence (`app/features/agent/persistence.py`)

All conversation state lives in Supabase. Two tables (both RLS-protected via `is_tutor()`):
- **`agent_conversations`** — one row per conversation, holds `lg_contents` and `prev_lg_contents` (both JSONB) for LLM history and one-level undo
- **`agent_messages`** — one row per message, holds `role`, `content`, `steps`, `is_error`, `students`, `schedule_students`, `slot_data`, and `created_at`

**Key functions:**
- `get_or_create_conversation(supabase)` — fetches the most recent conversation row (or creates one if the table is empty); returns `{ id, messages }` in a single call. Used by `GET /conversations/current`.
- `clear_conversation(supabase, conversation_id)` — deletes all messages and resets `lg_contents` and `prev_lg_contents` to null, keeping the same conversation row. Used by `POST /conversations/{id}/clear`.
- `pre_insert_agent_message(supabase, conversation_id)` — **write-ahead pattern**: inserts a placeholder agent row with `is_error=True` and empty content before any SSE byte is sent. Guarantees the row exists in DB even if the user reloads mid-stream. `on_complete`/`finally` then UPDATE this row with real content.
- `insert_user_message(supabase, conversation_id, content)` — inserts a user message row.
- `update_conversation_history(supabase, conversation_id, *, lg_contents, prev_lg_contents)` — updates LLM history columns; both params are keyword-only and optional — only columns whose param is not `None` are written. On each successful turn, the caller passes the current `lg_contents` as `prev_lg_contents` before overwriting — enabling one-level undo for edit. Also used for the **preemptive write** at the start of the edit path: called with just `lg_contents=lg_history_raw` before streaming starts, so that if the LLM fails, a subsequent retry reads the already-correct pre-edit context from `lg_contents` rather than the stale post-prior-turn value.
- `update_agent_message` / `insert_agent_message` — used by save paths; `update_agent_message` flips `is_error` to `False` on success.
- `_row_to_message(row)` — maps DB columns to the JSON shape sent to the frontend: `isError`, `steps`, `students`, `scheduleStudents`, `slotData`, `timestamp`.

**`effective_id` pattern:** all save paths use `effective_id = agent_msg_id_to_update or pre_agent_id` — cleanly unifies retry (existing row) and normal-send (pre-inserted row) without duplicating the update/insert branch logic. The actual write is done via `_persist_agent_message(sb, effective_id, conversation_id, **kwargs)` — a helper in `router.py` that calls `update_agent_message` when `effective_id` is set and `insert_agent_message` otherwise, used in all three save paths (on_complete, except, finally).

---

## Conversation management endpoints (`app/features/agent/router.py`)

| Method | Path | Description |
|---|---|---|
| `GET` | `/conversations/current` | Returns (or creates) the single latest conversation + its messages |
| `GET` | `/conversations/{id}/messages` | Fetch messages for a known conversation ID (used by `shouldReloadRef` after a turn) |
| `POST` | `/conversations/{id}/clear` | Delete all messages + reset LLM history columns |

---

## Supervisor routing rules (`app/features/agent/lg/supervisor.py`)

Key routing rules:

- Answer greetings / meta-questions / capability questions directly without routing (1–2 short sentences)
- Payment messages always require a student UUID: dispatch to `student_agent` first if only a name is known, then in a separate `dispatch` call route to `template_agent` with the UUID in the task
- Relay subagent replies verbatim — never output "Successfully transferred back to supervisor"
- All parallel tasks (same-domain or cross-domain) go into ONE `dispatch` call with multiple entries — the `dispatch` tool is the single routing mechanism
- Same agent, multiple entities → ONE combined entry (subagent batches tool calls internally). Different agents → one entry each (parallel via `Send` fan-out)
- **Never expand or guess student names** — copy the exact name or partial name the user typed; `search_students` does partial matching so "Ang" is a valid task input
- **UUID propagation:** scan prior replies for `[student_id:NAME:UUID]` tokens and embed known UUIDs directly in task descriptions (e.g. `"Update Ang (id: 2dfa867c-...) fee to 60"`) so the student subagent can call `update_student` without a redundant `search_students` round

---

## LangGraph multi-agent graph

```
START → supervisor ──dispatch──► student_agent  ──► supervisor → END
                              ├─► template_agent ──► supervisor
                              └─► timetable_agent──► supervisor
```

- **`lg/supervisor.py`** — `make_supervisor(supabase, date_string)` + `build_custom_supervisor()`: single `dispatch` tool (`lg/handoff.py`) with `handoffs: [{agentName, task}]`. Parallel dispatch is a single `Command` with multiple `Send` targets. One LLM call per supervisor turn. **Same-agent dedup:** `supervisor_node` groups entries by `agentName` in a `merged` dict, joining tasks with `\n` before emitting `Send` commands. **UUID propagation:** supervisor scans prior replies for `[student_id:NAME:UUID]` tokens and embeds known UUIDs in task descriptions. **Silent relay guarantee:** see `docs/decisions.md`.

- **`lg/subagent.py`** — `build_subagent()` creates a standard ReAct graph. `TERMINAL_TOOLS = {"final_answer", "cannot_complete"}`. After `tools`/`post_hook`, `route_after_tools` checks the last AIMessage's tool_calls — if any name is in `TERMINAL_TOOLS`, routes to `END` (skipping the next agent LLM call); otherwise routes back to `agent`. `should_continue` on the `agent` node is a non-compliance fallback (no tool calls → `END` without entering `ToolNode`).

- **`lg/tool_factories.py`** — `make_student_tools()`, `make_template_tools()`, `make_timetable_tools()`: wrap the shared tool implementations in Pydantic schemas for LangGraph. Each factory appends `make_cannot_complete_tool()` and `make_final_answer_tool()`.

- **Subagents** (`student_agent.py`, `template_agent.py`, `timetable_agent.py`): each wraps its tool set with a domain-specific system prompt. All three use markdown-only formatting (never HTML). `student_agent` is instructed to use UUIDs from task descriptions directly (skip `search_students`) and always append `[student_id:NAME:UUID]` tokens in replies involving `get_student`, `create_student`, or `update_student`. All three subagents call `cannot_complete(reason=...)` on tool mismatch and `final_answer(text=...)` as the mandatory ending. `make_call_agent` in `supervisor.py` extracts the reply from the `final_answer`/`cannot_complete` ToolMessage first, falling back to a free-text AIMessage.

- **`lg/agent_state.py`** — `AgentState`: `messages: Annotated[list[BaseMessage], add_messages]` + `audit_log: Annotated[list[str], operator.add]`. `audit_log` accumulates self-eval verdicts across tool rounds and is never sent to the LLM.

- **`lg/utils.py`** — `extract_text(msg)`: shared helper extracting plain text from an `AIMessage` / `AIMessageChunk`, handling both `str` and `list` content formats. Imported by `supervisor.py` and `stream_adapter.py`.

- **`lg/post_hooks.py`** — `make_student_post_hook()`, `make_timetable_post_hook()`: run `self_eval` for mutations in the current tool round in parallel via `asyncio.gather` and return `{"audit_log": [combined_verdict]}`. `_find_round_mutation_calls` scans backward for the last AIMessage with tool_calls (current round only).

- **`lg/stream_adapter.py`** — `pipe_langgraph_stream()` translates LangGraph `(namespace, mode, data)` event tuples into the shared SSE event types. In `updates` mode, drains `audit_log` from each node's output and emits verdicts as SSE `step` events. `is_routing_relevant()` filters what gets stored in LLM history — see `docs/decisions.md`. Calls `on_complete(accumulated_messages)` before emitting `done`; `on_complete` saves the agent message to DB and updates `lg_contents` on the conversation row.

- **`lg/model.py`** — `get_gemini_chat_model()` returns a fresh `ChatGoogleGenerativeAI` instance per call (`gemini-2.5-flash`, `temperature=0`, `thinking_budget=0`). Parallel subagents must not share a model instance — hence a new instance is constructed each time.

---

## SSE event contract

`POST /agent/chat` emits these event types:

| Event type | When | Payload |
|---|---|---|
| `step` | After each tool call (and after self-eval) | `{ type: "step", content: "🔧 tool_name({...})" }` |
| `chunk` | Streaming text tokens | `{ type: "chunk", content: "..." }` |
| `ui_action` | After `generate_slot_availability` or `download_timetable_image`, or after student create/update | `{ type: "ui_action", action: "slots_ready" \| "download_schedule" \| "student_links", payload: {...} }` |
| `done` | Stream complete (after DB save) | `{ type: "done" }` |
| `stopped` | User-initiated stop | `{ type: "stopped" }` |
| `error` | Unhandled exception | `{ type: "error", message: "..." }` |

`ui_action` uses a generic envelope with an `action` discriminator — adding a new UI-trigger tool only requires a new `action` value, not a new SSE event type.

**LangGraph (`tool_factories.py` + `stream_adapter.py`):** `generate_slot_availability` and `download_timetable_image` call `config.writer({"ui_action": {...}})` inside the tool wrapper; `pipe_langgraph_stream` forwards `custom` mode events as `ui_action` SSE events.

---

## Framework choices and trade-offs

### Why LangGraph

The LangGraph path uses `langchain-google-genai` (`ChatGoogleGenerativeAI`) behind LangChain's `BaseChatModel` interface. This was chosen for multi-agent orchestration because:

- Parallel dispatch via `Send` fan-out is built-in
- State accumulation (`add_messages`, `operator.add`) is handled by the graph runner
- Switching provider is one line — `ChatGoogleGenerativeAI` → `ChatAnthropic` → `ChatOpenAI` — all implement the same `BaseChatModel` interface; graph, tools, stream adapter unchanged

**Switching cost if using LangChain:** change the import and class name in `lg/model.py` — ~30 minutes regardless of provider.

### Multi-agent framework landscape (as of 2025)

| Framework | Abstraction | Debugging | Vendor lock | Production use |
|---|---|---|---|---|
| **CrewAI** | Very high (role/goal/backstory) | Hard | No | Low — popular for demos, rare in production |
| **LangGraph** | Medium (graph nodes + edges) | Medium | No | High today, losing ground |
| **AutoGen** (Microsoft) | Medium (conversational agents) | Hard | No | Research/academic, less production |
| **OpenAI Agents SDK** | Low (Agent + handoff + Runner) | Easy | OpenAI only | Growing fast among OpenAI shops |
| **Raw SDK** | None | Easy | Per-provider | Where mature teams end up |

The typical industry trajectory: **CrewAI demo → LangGraph prototype → raw SDK production.** Teams that started with raw SDK never needed to rewrite.

LangGraph's graph abstraction helps for simple linear flows but fights you on complex dynamic routing — the custom supervisor in `lg/supervisor.py` (manual `Command` + `Send` construction) is evidence of working around the abstraction rather than with it. The alternative would be a plain `asyncio.gather` over subagent coroutines, which is essentially what LangGraph executes under the hood.

LangGraph remains the right choice here because: (1) the LangGraph path is already built and working, (2) provider portability is a real benefit, (3) the use case (3 subagents, parallel dispatch, simple routing) does not push against LangGraph's limits.
