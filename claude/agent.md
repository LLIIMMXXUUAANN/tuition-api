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
- `download_timetable_image` fetches active students' `name` and `class_schedule` ordered by name; returns `{ students }` which the router emits as a `ui_action` SSE event — the frontend renders the PNG client-side

---

## System instruction rules (classic agent, `app/features/agent/schema.py`)

1. Reuse UUID from conversation history — only call `search_students` if UUID not already known
2. `delete_student` requires explicit "yes" in conversation; must warn about Calendar/Drive removal first
3. Ask for missing required fields (`mode`, `fee_per_hour`) before calling `create_student`
4. Multiple search matches → list and ask which student
5. No search results for update/delete → say so, offer to create instead
6. After create/update → append one `[student_id:NAME:UUID]` token per affected student at the end of the reply (frontend renders a "View NAME →" link per token). Example for two students: `[student_id:Lynn:uuid-1] [student_id:Ang:uuid-2]`
7. Formatting rules: tables for lists, bold labels for single records, skip null/empty fields, render Meet/Drive as markdown links, blockquote for notes/homework, `list_students` for roster queries
8. `sync_all_students` requires explicit confirmation before calling
9. Delete confirmation must mention Google Calendar/Drive removal
10. `get_schedule`: resolve "today"/"tomorrow" using injected date; format as Name | Time table (12-hour); say "No classes on [day]" if empty
11. `get_fee_summary`: use for any revenue/fee/income query (all students or a specific student); omit month/year if not specified; format as Name | Fee (RM) table with bold Total row; for single-student query, find the student in the returned list and report only their fee
12. When the user's request involves multiple independent operations, call all relevant tools in a single round. Only serialise when one call's output is required as input for the next.
13. Templates: call `get_template` directly when the template is clear (e.g. "first approach", "payment"); call `list_templates` first only when ambiguous. Display template as bold title on its own line, then content in a fenced code block (no language tag).
14. `generate_payment_message`: use when the user asks to generate a payment message/reminder for a student. Omit month/year if not specified (defaults to next month). Ask about carryover only if the user mentions it — otherwise default to `template_type 1`. Display result as bold header (e.g. "**Payment reminder — June 2026**") then message in a fenced code block.
15. Timetable settings: use `get_timetable_settings` to read current rules and buffer before updating. When the user asks to update rules, show them the proposed new rules and confirm before calling `update_timetable_rules`. For `update_buffer_mins`, validate 0–60 before calling.
16. After calling `generate_slot_availability` or `download_timetable_image`, tell the user a download button has appeared in the chat. Do NOT describe slot counts or classification details unless the user asks — keep the reply brief (one sentence).

---

## Supervisor routing rules (LangGraph mode, `app/features/agent/lg/supervisor.py`)

Key additions and overrides vs. the classic agent rules:

- Answer greetings / meta-questions / capability questions directly without routing (1–2 short sentences)
- Payment messages always require a student UUID: dispatch to `student_agent` first if only a name is known, then in a separate `dispatch` call route to `template_agent` with the UUID in the task
- Relay subagent replies verbatim — never output "Successfully transferred back to supervisor"
- All parallel tasks (same-domain or cross-domain) go into ONE `dispatch` call with multiple entries — the `dispatch` tool is the single routing mechanism
- Same agent, multiple entities → ONE combined entry (subagent batches tool calls internally). Different agents → one entry each (parallel via `Send` fan-out)
- **Never expand or guess student names** — copy the exact name or partial name the user typed; `search_students` does partial matching so "Ang" is a valid task input
- **UUID propagation:** scan prior replies for `[student_id:NAME:UUID]` tokens and embed known UUIDs directly in task descriptions (e.g. `"Update Ang (id: 2dfa867c-...) fee to 60"`) so the student subagent can call `update_student` without a redundant `search_students` round

---

## Classic loop details (`app/features/agent/router.py`)

- Current MYT date is prepended to `SYSTEM_INSTRUCTION` at request time via Python's `datetime` + `pytz` (`pytz.timezone("Asia/Kuala_Lumpur")`) so the LLM can resolve "today"/"tomorrow" before calling `get_schedule` — the injected string is formatted as `"Today is {weekday}, {date} (Malaysia Time)."`.
- Runs up to 10 rounds. Tool-calling rounds execute all function calls from the current round in parallel via `asyncio.gather`; each call emits a `step` SSE event immediately. The final text-only round streams `chunk` events token by token.
- Soft stop check at the start of each round: if `stop_signals.get(request_id)` is set, breaks cleanly without interrupting a mid-tool call.
- `MUTATION_TOOLS = {"update_student", "delete_student", "update_timetable_rules", "update_buffer_mins", "manage_portal_access"}` — module-level constant used to track which mutations occurred for `self_eval`. `create_student` is tracked separately (check for `result.get("id")`) rather than via this set.
- `self_eval` runs after each tool round that contains mutations, before moving to the next round. Stop checks happen at the top of each round, so a stopped request receives self-eval results only for rounds that completed before the stop.

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

- **Subagents** (`student_agent.py`, `template_agent.py`, `timetable_agent.py`): each wraps its tool set with a domain-specific system prompt. `student_agent` is instructed to use UUIDs from task descriptions directly (skip `search_students`) and always append `[student_id:NAME:UUID]` tokens in replies involving `get_student`, `create_student`, or `update_student`. All three subagents call `cannot_complete(reason=...)` on tool mismatch and `final_answer(text=...)` as the mandatory ending. `make_call_agent` in `supervisor.py` extracts the reply from the `final_answer`/`cannot_complete` ToolMessage first, falling back to a free-text AIMessage.

- **`lg/agent_state.py`** — `AgentState`: `messages: Annotated[list[BaseMessage], add_messages]` + `audit_log: Annotated[list[str], operator.add]`. `audit_log` accumulates self-eval verdicts across tool rounds and is never sent to the LLM.

- **`lg/utils.py`** — `extract_text(msg)`: shared helper extracting plain text from an `AIMessage` / `AIMessageChunk`, handling both `str` and `list` content formats. Imported by `supervisor.py` and `stream_adapter.py`.

- **`lg/post_hooks.py`** — `make_student_post_hook()`, `make_timetable_post_hook()`: run `self_eval` for mutations in the current tool round in parallel via `asyncio.gather` and return `{"audit_log": [combined_verdict]}`. `_find_round_mutation_calls` scans backward for the last AIMessage with tool_calls (current round only).

- **`lg/stream_adapter.py`** — `pipe_langgraph_stream()` translates LangGraph `(namespace, mode, data)` event tuples into the shared SSE event types. In `updates` mode, drains `audit_log` from each node's output and emits verdicts as SSE `step` events. `is_routing_relevant()` filters what gets stored in `lgHistory` — see `docs/decisions.md`.

- **`lg/model.py`** — `get_gemini_chat_model()` returns a fresh `ChatGoogleGenerativeAI` instance per call (`gemini-2.5-flash`, `temperature=0`, `thinking_budget=0`). Parallel subagents must not share a model instance — hence a new instance is constructed each time.

---

## SSE event contract

Both `POST /agent/chat` (classic) and `POST /agent/lg/chat` (LangGraph) emit the same event types:

| Event type | When | Payload |
|---|---|---|
| `step` | After each tool call (and after self-eval) | `{ type: "step", content: "🔧 tool_name({...})" }` |
| `chunk` | Streaming text tokens | `{ type: "chunk", content: "..." }` |
| `ui_action` | After `generate_slot_availability` or `download_timetable_image` | `{ type: "ui_action", action: "slots_ready" \| "download_schedule", payload: {...} }` |
| `history` | After classic loop completes cleanly | `{ type: "history", contents: Content[] }` — full Gemini `Content[]` (tool-call parts included) |
| `lg_history` | After LangGraph loop completes cleanly | `{ type: "lg_history", messages: StoredMessage[] }` — routing-level messages only |
| `done` | Stream complete | `{ type: "done" }` |
| `stopped` | User-initiated stop | `{ type: "stopped" }` |
| `error` | Unhandled exception | `{ type: "error", message: "..." }` |

`ui_action` uses a generic envelope with an `action` discriminator — adding a new UI-trigger tool only requires a new `action` value, not a new SSE event type or new frontend handler branch.

**Classic loop (`router.py`):** `execute_tool` accepts an optional `side_effects: list[dict]` parameter; the two special match cases (`generate_slot_availability`, `download_timetable_image`) append their `ui_action` dict to the list if the result contains the expected key. The main loop drains it with `yield` after all tools in a round complete.

**LangGraph (`tool_factories.py` + `stream_adapter.py`):** `generate_slot_availability` and `download_timetable_image` call `config.writer({"ui_action": {...}})` inside the tool wrapper; `pipe_langgraph_stream` forwards `custom` mode events as `ui_action` SSE events.
