# Backend Design Decisions

Non-obvious decisions and the reasoning behind them. Code-level detail lives in `claude/`.

---

## Agent

**Self-evaluation after mutations (`eval.py`)**

After any tool round that includes a write operation, `self_eval` runs a read-back query against Supabase to verify the mutation landed:
- `create_student` / `update_student` → SELECT by id, confirm row exists
- `delete_student` → SELECT by id, confirm row is gone
- `update_timetable_rules` → read back from `settings`, compare to what was written
- `update_buffer_mins` → read back from `settings`, compare parsed integer

**Per-round, not post-loop.** `self_eval` runs inside the tool-execution loop — once per tool round, covering all mutations from that round in parallel via `asyncio.gather`. If the agent updates two students in one round (the system prompt encourages batching), both are verified simultaneously.

**Passive audit (Option A) — results shown to the user, never fed back to the agent.** A successful verification appears as `✓ verified in DB`; a failure appears as `⚠ could not verify`. The agent never sees these verdicts and cannot retry based on them.

This is the standard approach for interactive agents: transient infrastructure failures (a Supabase read racing against a just-completed write) should not trigger agent retries that risk duplicate writes. Feeding verification failures back into the LLM loop (Option B) treats a monitoring concern as an agent-control concern, conflates two responsibilities, and introduces the risk of write amplification.

**LangGraph:** `post_hooks.py` writes verdicts to `audit_log: Annotated[list[str], operator.add]` in `AgentState` — never injected into the message list, so verdicts never reach any `model.invoke()` call. `stream_adapter.py` drains `audit_log` from each node's output and emits verdicts as SSE `step` events.

---

**Custom supervisor instead of `langgraph-supervisor` (`supervisor.py`)**

The official `langgraph-supervisor` package was replaced with `build_custom_supervisor` to fix two specific issues:

1. **Echoing.** The official package echoes the handoff ToolMessage content (`"Successfully transferred back to supervisor"`) as the supervisor's reply instead of forwarding the subagent's actual answer. The custom supervisor puts the real reply in the ToolMessage and has the supervisor LLM relay it verbatim.

2. **Double LLM call per supervisor turn.** The official package wraps the supervisor in `createReactAgent`, which always makes two LLM calls per turn (LLM → tool → LLM again to "check if done"). A routing supervisor makes exactly one decision per turn — the second call is pure waste. The custom `supervisor_node` calls the LLM once and returns immediately.

---

**LangGraph dispatch reliability**

Two structural invariants are enforced in code rather than relying solely on prompt rules:

**Same-agent dedup.** When the LLM creates two separate `dispatch` entries for the same agent, `supervisor_node` merges them: after normalising `handoff_list`, a `merged: dict[str, str]` groups entries by `agentName` and joins tasks with `\n`. Only then are `Send` commands emitted — guaranteeing one subagent invocation per agent regardless of LLM compliance.

**UUID propagation.** `build_supervisor_prompt` instructs the supervisor to scan prior replies for `[student_id:NAME:UUID]` tokens and embed known UUIDs in task descriptions (e.g. `"Update Ang (id: 2dfa867c-...) fee to 60"`). `STUDENT_PROMPT` instructs the student_agent: if the task contains a UUID in parentheses, call `update_student` directly without a `search_students` round. The student_agent appends `[student_id:NAME:UUID]` tokens to every reply involving `get_student`, `create_student`, or `update_student`; these flow into `lgHistory` via `is_routing_relevant`, making UUIDs available for subsequent turns.

---

**Supervisor silent relay guarantee (`supervisor.py`)**

When a subagent completes and hands back to the supervisor, the supervisor must relay the reply verbatim. Three complementary layers enforce this:

**`content=""` on handoff AIMessage.** `_create_handoff_back_messages` sets the AIMessage content to `""`. The prior value (`"Transferring back to supervisor"`) was a model-role message that Gemini mistook for its own prior output — causing it to consider its turn "already done" and produce no new text.

**Code fallback in `supervisor_node`.** If the LLM produces no `dispatch` call and no text, `supervisor_node` scans backward through state messages for the most recent `transfer_back_to_*` ToolMessages and joins their content as the reply. Prompts define desired behaviour; code enforces invariants.

**CRITICAL prompt rule.** `build_supervisor_prompt` ends with: every supervisor turn must produce either a `dispatch` call or non-empty text; empty output is never valid. The code fallback is authoritative (never fails); the prompt rule reduces how often it's needed.

---

**Subagent terminal tools (`tool_factories.py` + `subagent.py`)**

Each subagent has two terminal tools that end its turn without an extra LLM summarisation call:

**`final_answer(text)`** — normal exit. The subagent calls this with its complete reply. `route_after_tools` in `subagent.py` detects it and routes to `END` immediately — saving one LLM call per subagent invocation. The ToolMessage content becomes the reply in `make_call_agent`.

**`cannot_complete(reason)`** — failure exit. Called when the task doesn't match available tools. Gives the supervisor a clear `"Cannot complete: ..."` reason rather than vague free text.

`should_continue` on the `agent` node is retained as a non-compliance fallback: if the LLM outputs no tool calls at all, route to `END` without entering `ToolNode` (which would crash on empty tool calls). The supervisor does not get these tools — it is a single-turn routing node, not a ReAct loop.

---

**UI side-effect events — `ui_action` generic envelope**

Two tools (`generate_slot_availability`, `download_timetable_image`) trigger frontend UI events — a download button appears in the chat after they run. Emitted as a generic SSE envelope:

```json
{"type": "ui_action", "action": "slots_ready",       "payload": {"slots": [...]}}
{"type": "ui_action", "action": "download_schedule",  "payload": {"students": [...]}}
```

One envelope type with an `action` discriminator means adding a new UI-trigger tool only requires a new `action` value — no new SSE event types, no new frontend handler branches.

The coupling between "which tools trigger UI events" and SSE emission is in `execute_tool`'s match block (classic) and `tool_factories.py`'s `config.writer` callback (LangGraph) — not in the main streaming loop.

---

**Why LangGraph history omits subagent-internal tool calls (`is_routing_relevant`)**

`lgHistory` (stored client-side, sent on every request) contains only routing-level messages. Subagent-internal tool call pairs (e.g. `search_students → result → get_student → result` inside `student_agent`) are stripped by `is_routing_relevant` before the `lg_history` SSE event is emitted.

1. **They are ephemeral implementation detail, not conversation state.** The supervisor dispatched a task and received a conclusion. The specific DB queries are no longer load-bearing for future routing decisions.
2. **Including them grows history proportionally to tool call depth.** A single subagent invocation can involve 3–6 tool call/response pairs — quickly increasing the token cost of every subsequent request.
3. **They can mislead the supervisor across turns.** Stale intermediate results from two turns ago could cause the supervisor to re-reason from old data instead of issuing a fresh lookup.
4. **The supervisor has enough context to re-derive what it needs.** If the user says "do the same for Ang", the supervisor sees it previously dispatched to `student_agent` and got a reply — it dispatches again and the subagent makes fresh DB calls.

`is_routing_relevant` keeps: `HumanMessage`, supervisor `AIMessage` with `dispatch` tool call + paired `ToolMessage`, `transfer_back_to_supervisor` AIMessage + ToolMessage pairs (subagent's final reply), supervisor `AIMessage` with no tool calls (direct reply). All `SystemMessage` entries and subagent-internal messages are dropped.

---

**Why the agent is stateless (no LangGraph checkpointer)**

Both backends send conversation history from the client on every request rather than persisting it server-side via a checkpointer. The primary reason: there is only one admin with no concurrent sessions. Stateful checkpointing (`MemorySaver`, a Postgres checkpointer, etc.) is designed for many users maintaining long-running threads that need to survive browser refreshes and be resumed across devices. For a single user whose history already lives in localStorage and is sent back on every request, the infrastructure overhead — external store, thread ID management, TTL/cleanup — provides no benefit.

The frontend stores two localStorage keys: `agent_gemini_contents` (full Gemini `Content[]` for the classic loop) and `agent_lg_contents` (routing-level LangGraph messages, filtered by `is_routing_relevant`). Both are sent back on every request to reconstruct conversation context.

---

## Auth

**Service identity: `X-Internal-Secret` instead of service account JWTs**

FastAPI is a private internal service — not reachable from the internet. All callers must present an `X-Internal-Secret` header (verified by `require_internal_secret` in `auth.py`, applied at the router level via `dependencies=[Depends(...)]`). This is a simplified form of service identity: only the Next.js server knows the secret, so FastAPI knows any request carrying it came from the trusted frontend.

The industry-standard pattern for service-to-service auth uses one of:
- **OAuth2 client credentials flow** — the calling service exchanges a `client_id` + `client_secret` for a short-lived JWT from a dedicated auth server (Keycloak, Auth0, etc.)
- **mTLS** — mutual TLS where both services present certificates; used internally at Google, Netflix, and Uber via a service mesh (Istio/Envoy)
- **Self-signed service JWTs** — the calling service signs its own JWT with a private key; the receiving service verifies with the public key

All three options give rotating, auditable, machine-identity credentials. `X-Internal-Secret` is a static shared secret — simpler but acceptable here because: (1) a single admin, no multi-tenancy; (2) FastAPI is not internet-exposed; (3) Supabase RLS enforces data-access rules independently if the secret were ever compromised.

**When to upgrade:** introduce a second frontend (mobile app, third-party integration) or expose FastAPI on a public endpoint. At that point, replace `X-Internal-Secret` with OAuth2 client credentials so each caller has its own rotating identity.

---

**User identity stops at Next.js — FastAPI has no per-user auth**

The Supabase user JWT exists in the browser cookie and is verified by Next.js (`requireTutor()` calls `supabase.auth.getUser()` + the `is_tutor()` RPC). The JWT is never forwarded to FastAPI — FastAPI receives only `X-Internal-Secret` and has no visibility into which user made the request.

In the full industry pattern, the Next.js proxy would also forward `Authorization: Bearer <user-jwt>`, and FastAPI would verify it independently using the Supabase JWT secret (HS256, verifiable with `python-jose` without a Supabase network call). This gives FastAPI the caller's `sub`, `email`, and `role` for per-user logging, auditing, and fine-grained access control.

The two caller types and their auth in the full pattern:

| Caller | How it reaches FastAPI | User identity | Service identity |
|---|---|---|---|
| Browser (mutations) | Next.js catch-all proxy | User JWT (`Authorization: Bearer`) | `X-Internal-Secret` |
| Server Component (reads) | `fetchFastAPI` directly | Service account JWT | `X-Internal-Secret` |

This project omits user JWT forwarding and uses no service account JWT for server-side calls because: (1) single tutor, no per-user audit requirement; (2) Supabase does not issue service account tokens — implementing service JWTs would require an additional auth server; (3) all Server Component calls are reads, already protected by Next.js middleware before the component renders.

**When to upgrade:** add a second admin, require per-user audit logs, or expose any write endpoint to a non-Next.js caller. Forward the Supabase JWT in the proxy and add `require_jwt` to mutation endpoints.

---

## Not implemented (future reference)

**Prompt caching**

Gemini context caching can cache the static prefix (system instruction + tool declarations) at a reduced token rate. Not used because:
- **Prefix is too small.** System instruction + 18 tool declarations is ~2,000–4,000 tokens — a fraction of a cent saving per request at Gemini 2.5 Flash pricing.
- **Single admin, low volume.** Cache hits require the cache to stay warm (TTL ≥ 1 min). Occasional usage means mostly cold-cache requests.
- **Intra-request benefit is modest.** The classic loop runs 2–3 rounds typically — too small a multiplier to justify lifecycle complexity.

**When to add it:** inject large static documents (curriculum, full student roster, multi-page scheduling rules) into the system prompt. At 50k+ tokens the ~4× cached-token discount becomes material. Create a module-level cache object and invalidate it on content change.

---

**Tool retrieval**

At 50–100+ tools, the industry uses embedding-based RAG to fetch only the most relevant tool schemas per query. At 18 tools this is unnecessary — all schemas fit comfortably in a single prompt. The LangGraph mode narrows each subagent's view to 3–11 tools via static domain partitioning, achieving the same scoping benefit without embeddings.

---

**Raw SDK over MCP / CLI**

All service integrations (Supabase, Google Drive/Calendar, Gemini) use their Python SDKs directly.

- **MCP** is designed for exposing tools to a remotely-running AI model or in a multi-user environment with isolated tool context. Neither applies here — one admin, route handlers and tool calls run in the same process.
- **CLI** assumes a shell environment invocable per-request. A FastAPI service on a cloud platform is not that shell.
- **Raw SDK calls** are the natural fit: no extra infrastructure, full Python type stubs, straightforward async error handling, no abstraction layer.
