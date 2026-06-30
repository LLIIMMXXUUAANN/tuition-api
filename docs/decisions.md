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

`post_hooks.py` writes verdicts to `audit_log: Annotated[list[str], operator.add]` in `AgentState` — never injected into the message list, so verdicts never reach any `model.invoke()` call. `stream_adapter.py` drains `audit_log` from each node's output and emits verdicts as SSE `step` events.

---

**Custom supervisor instead of `langgraph-supervisor` (`supervisor.py`)**

The official `langgraph-supervisor` package was replaced with `build_custom_supervisor` to fix two specific issues:

1. **Echoing.** The official package echoes the handoff ToolMessage content (`"Successfully transferred back to supervisor"`) as the supervisor's reply instead of forwarding the subagent's actual answer. The custom supervisor puts the real reply in the ToolMessage and has the supervisor LLM relay it verbatim.

2. **Double LLM call per supervisor turn.** The official package wraps the supervisor in `createReactAgent`, which always makes two LLM calls per turn (LLM → tool → LLM again to "check if done"). A routing supervisor makes exactly one decision per turn — the second call is pure waste. The custom `supervisor_node` calls the LLM once and returns immediately.

---

**LangGraph dispatch reliability**

Two structural invariants are enforced in code rather than relying solely on prompt rules:

**Same-agent dedup.** When the LLM creates two separate `dispatch` entries for the same agent, `supervisor_node` merges them: after normalising `handoff_list`, a `merged: dict[str, str]` groups entries by `agentName` and joins tasks with `\n`. Only then are `Send` commands emitted — guaranteeing one subagent invocation per agent regardless of LLM compliance.

**UUID propagation.** `build_supervisor_prompt` instructs the supervisor to scan prior replies for `[student_id:NAME:UUID]` tokens and embed known UUIDs in task descriptions (e.g. `"Update Ang (id: 2dfa867c-...) fee to 60"`). `STUDENT_PROMPT` instructs the student_agent: if the task contains a UUID in parentheses, call `update_student` directly without a `search_students` round. The student_agent appends `[student_id:NAME:UUID]` tokens to every reply involving `get_student`, `create_student`, or `update_student`; these flow into the persisted LG history via `is_routing_relevant`, making UUIDs available for subsequent turns.

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

The coupling between "which tools trigger UI events" and SSE emission is in `tool_factories.py`'s `config.writer` callback — not in the main streaming loop.

---

**Why LangGraph history omits subagent-internal tool calls (`is_routing_relevant`)**

`lg_contents` (stored server-side in `agent_conversations`, restored at the start of each request) contains only routing-level messages. Subagent-internal tool call pairs (e.g. `search_students → result → get_student → result` inside `student_agent`) are stripped by `is_routing_relevant` before `on_complete` saves them.

1. **They are ephemeral implementation detail, not conversation state.** The supervisor dispatched a task and received a conclusion. The specific DB queries are no longer load-bearing for future routing decisions.
2. **Including them grows history proportionally to tool call depth.** A single subagent invocation can involve 3–6 tool call/response pairs — quickly increasing the token cost of every subsequent request.
3. **They can mislead the supervisor across turns.** Stale intermediate results from two turns ago could cause the supervisor to re-reason from old data instead of issuing a fresh lookup.
4. **The supervisor has enough context to re-derive what it needs.** If the user says "do the same for Ang", the supervisor sees it previously dispatched to `student_agent` and got a reply — it dispatches again and the subagent makes fresh DB calls.

`is_routing_relevant` keeps: `HumanMessage`, supervisor `AIMessage` with `dispatch` tool call + paired `ToolMessage`, `transfer_back_to_supervisor` AIMessage + ToolMessage pairs (subagent's final reply), supervisor `AIMessage` with no tool calls (direct reply). All `SystemMessage` entries and subagent-internal messages are dropped.

---

**Why the agent uses custom persistence instead of a LangGraph checkpointer**

Conversation history is persisted server-side in Supabase (`agent_conversations` + `agent_messages`) rather than via a LangGraph checkpointer (`MemorySaver`, a Postgres checkpointer, etc.). The frontend stores no history — it calls `GET /conversations/current` on mount to receive both the conversation ID and its messages in one round trip.

LangGraph checkpointers are designed for managing many concurrent threads, each with its own `thread_id`, TTL, and cleanup lifecycle. For a single admin with one active conversation at a time, that infrastructure adds complexity without benefit.

The custom persistence layer (`persistence.py`) also enables features that checkpointers don't provide out of the box:
- **Pre-insert write-ahead pattern** — agent row inserted with `is_error=True` before SSE starts; guarantees the row exists even if the user reloads mid-stream
- **`prev_lg_contents`** on the conversation row — updated every successful turn with the prior state, enabling one-level undo for latest-message edit. The earlier design stored a `pre_turn_llm_snapshot` on every user message row to support retrying any historical error, not just the latest one. That per-row snapshot was dropped once retry and edit were restricted to the latest message only (consistent with how Claude.ai behaves): failed turns never update `lg_contents`, so `lg_contents` is already the correct pre-failure state for retry; edit only ever reaches one turn back, so a single `prev_lg_contents` column on the conversation row is sufficient. No per-message snapshot overhead needed.
- **Preemptive write on edit** — at the start of the edit path, `lg_contents` is immediately reset to the pre-edit value (`prev_lg_contents`) before streaming begins. This ensures that if the LLM fails during an edit, the subsequent retry reads the correct pre-edit context — not the stale post-prior-turn state. The alternative (no preemptive write) would leave `lg_contents` pointing at stale history if the edit fails, causing retry to use the wrong context, including across page reloads.
- **Cross-device access** — any device calling `GET /conversations/current` gets the same history, since there is no localStorage dependency
- **`clear_conversation`** — wipes messages and resets `lg_contents`/`prev_lg_contents` on the same row, keeping the conversation ID stable

**When to add `AsyncPostgresSaver`:** if human-in-the-loop approval flows are needed (e.g. agent proposes a schedule change, tutor clicks Approve before it's committed), add `AsyncPostgresSaver` pointing at Supabase alongside the existing tables — not replacing them. The checkpointer handles graph resumption from a specific node boundary; `agent_messages` stays as-is for the chat UI. The two layers coexist independently.

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

**`Depends(get_supabase)` on every endpoint**

All endpoints inject the Supabase client via FastAPI's dependency injection rather than calling `await get_supabase()` directly inside the function body:

```python
async def list_students(supabase: AsyncClient = Depends(get_supabase)):
    ...
```

FastAPI calls `get_supabase()` automatically before the endpoint runs and passes the result in as a parameter. This is the industry-standard pattern — it makes dependencies explicit, enables FastAPI to handle lifecycle cleanup, and makes endpoints easier to test by swapping the dependency with a mock.

**SSE generator (`event_gen`) in `agent/router.py`:** the inner generator receives `supabase` as an explicit parameter (`sb: AsyncClient`) rather than capturing it from the outer scope via closure. It is called as `EventSourceResponse(event_gen(supabase))`. This makes the generator's dependency explicit and testable in isolation, consistent with the same principle.

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

## API: snake_case convention

**The backend always speaks pure snake_case — in both directions.**

All JSON response bodies, SSE event payload keys, and Pydantic request model field names use snake_case. No `Field(alias=...)` camelCase aliases, no camelCase Pydantic field names, no camelCase keys in `json.dumps(...)` calls.

This is the standard Python/FastAPI convention. The frontend owns all camelCase conversion at its own fetch boundaries — the backend has no knowledge of or responsibility for the frontend's naming preference.

**Request bodies** — the frontend sends snake_case to the backend (converted from camelCase at the point of submission). No Pydantic aliases are needed because the wire format matches the model field names.

**SSE events** — payload keys are snake_case (e.g. `student_links`, `students`, `slots`). Event `type` and `action` string values (e.g. `"ui_action"`, `"student_links"`, `"chunk"`) are also snake_case, consistent with Python convention. String values are not JSON object keys — they pass through conversion utilities unchanged on the frontend.

**Agent tool result dicts** — internal dicts returned by tool functions to the LLM use snake_case keys (e.g. `google_warnings`, `month_name`). These are LLM-facing, not API-facing, but follow the same convention for consistency.

**Exception — LangGraph handoff schema:** `HandoffEntry.agentName` in `lg/handoff.py` is intentionally camelCase. This is the JSON schema the LLM must output when invoking the `dispatch` tool — LangGraph's tool-calling protocol, not a REST API field. Do not rename it.

---

**Future work — where larger teams go further:**

| What | Industry upgrade | Why |
|---|---|---|
| Hand-written Pydantic response models | Code-gen from DB schema (`supabase-pydantic`) | Models always in sync with schema; zero manual upkeep after column changes |
| FastAPI + OpenAPI spec | Already auto-generated by FastAPI — expose `/openapi.json` for consumers | Free contract documentation; enables client code generation for typed API clients |
| `X-Internal-Secret` shared secret | OAuth2 client credentials or mTLS | Rotating, auditable machine identity — needed when a second caller is added |
| snake_case-only request bodies | No change needed — backend is already pure snake_case | The frontend owns conversion (`decamelizeKeys` before send, `camelizeKeys` after receive); this is the correct split for a Python backend + TypeScript frontend |

**OpenAPI docs (`/docs`):** FastAPI auto-generates Swagger UI at `/docs` from route definitions and Pydantic models. Every `BaseModel` used as a request body or `response_model=` on a route decorator is listed in the Schemas section. Routes are grouped by `tags=` on each `APIRouter` — currently: `students`, `google`, `payment`, `templates`, `timetable`, `agent`. Health endpoints on the main app have no tag and appear under "default".

---

## Dual error shapes — HTTP vs tool errors

Two different error shapes are used intentionally, because they have different consumers:

- **FastAPI endpoint errors** — `{"detail": "..."}` via `HTTPException`. Consumed by the frontend (an HTTP client). This is the FastAPI/HTTP standard.
- **Agent tool errors** — `{"error": "..."}` returned as a plain dict from tool functions. Consumed by the LLM, which reads the field and decides how to respond to the user.

The LLM is not an HTTP client — it does not understand status codes or `{"detail": ...}`. Returning `{"error": "Student not found"}` is the de facto convention for LLM tool results recommended by OpenAI, Anthropic, and LangChain in their agent/function-calling docs. Standardising both to one shape would mean forcing either the frontend or the LLM to parse a format not meant for it.

---

## Not implemented (future reference)

**Prompt caching**

Gemini context caching can cache a static prefix (system prompt + tool schemas) at a reduced token rate (~4× cheaper for cached tokens). Not used because:
- **Prefixes are small.** Each subagent's system prompt + tool schemas is ~1,000–3,000 tokens — a fraction of a cent saving per request at Gemini 2.5 Flash pricing.
- **Single admin, low volume.** Cache hits require the cache to stay warm (TTL ≥ 1 min). Occasional usage means mostly cold-cache requests.
- **Short subagent turns.** Each subagent runs 1–3 tool calls typically — too small a multiplier to justify cache lifecycle complexity.

**When to add it:** inject large static documents (curriculum, full student roster, multi-page scheduling rules) into a subagent's system prompt. At 50k+ tokens the ~4× cached-token discount becomes material. In LangGraph, cache per-subagent: `ChatGoogleGenerativeAI` supports caching via the `cached_content` parameter — create a module-level cache per subagent and invalidate it when the system prompt changes (e.g. when timetable rules are updated).

---

**Tool retrieval**

At 50–100+ tools, the industry uses embedding-based RAG to fetch only the most relevant tool schemas per query. At 18 tools this is unnecessary — all schemas fit comfortably in a single prompt. The LangGraph mode narrows each subagent's view to 3–11 tools via static domain partitioning, achieving the same scoping benefit without embeddings.

---

**Conversation history summarisation**

When `lg_contents` grows large enough to approach the context window limit or noticeably increase per-request token cost, compress old turns via summarise-and-replace. Not needed now — one admin, conversation is cleared periodically, history is small.

**How to implement when needed:**

1. **Trigger** — before building the request payload, count tokens in the stored history (or use turn count as a cheaper proxy). If above threshold (e.g. 60k tokens), run summarisation.

2. **Summarise** — call a smaller/cheaper model (e.g. `gemini-2.5-flash-lite`) with the oldest N messages and a prompt like `"Summarise this conversation history concisely for an AI assistant's context"`. Use a cheap model — the task is simple and doesn't need the primary model.

3. **Replace in `lg_contents`** — discard the N old messages, prepend the summary as a single `SystemMessage` (or `HumanMessage`), keep the most recent turns verbatim (sliding window of ~10 turns). Save back to DB.

4. **`agent_messages` stays untouched** — the UI always shows the full conversation history. The two stores diverge intentionally: `agent_messages` is the display layer, `lg_contents` is the compressed LLM context layer.

5. **Hook point** — summarise inside `on_complete` in `stream_adapter.py` before saving updated `lg_contents` back to `agent_conversations`, or as a pre-request step at the top of the `agent_chat` endpoint.

**Compatibility with retry/edit:** since both are restricted to the latest message only, summarisation does not break them. Summarisation compresses old turns; `prev_lg_contents` always captures the state immediately before the latest turn — which is always in the unsummarised recent window. Retry uses `lg_contents` directly (failed turns never update it); edit reads `prev_lg_contents`. Neither operation ever needs to reach back past the summarisation boundary.

---

**Raw SDK over MCP / CLI**

All service integrations (Supabase, Google Drive/Calendar, Gemini) use their Python SDKs directly.

- **MCP** is designed for exposing tools to a remotely-running AI model or in a multi-user environment with isolated tool context. Neither applies here — one admin, route handlers and tool calls run in the same process.
- **CLI** assumes a shell environment invocable per-request. A FastAPI service on a cloud platform is not that shell.
- **Raw SDK calls** are the natural fit: no extra infrastructure, full Python type stubs, straightforward async error handling, no abstraction layer.

---

**Sentry (error tracking)**

The backend uses Python's standard `logging` module — `logger.exception(msg)` emits ERROR-level entries with a full traceback to stdout, which is captured by the host platform's log aggregator (Render, Railway, etc.). This is sufficient for a single-admin local deployment where the developer can watch the terminal.

Not used because:
- **Low volume, one admin.** Errors are noticed immediately; no need for automated alerting.
- **Stdout is enough.** The host platform's log tail is the only viewer.
- **Zero infra.** No Sentry project, DSN, or account needed.

**When to add it:** any time you deploy and need to be notified of production errors without watching the terminal — or when multiple users hit the service and errors need grouping, deduplication, and a searchable history.

**How to add it:**

```python
# requirements.txt
sentry-sdk[fastapi]>=2.0

# app/main.py  (top of file, before FastAPI app is created)
import sentry_sdk
sentry_sdk.init(
    dsn=settings.SENTRY_DSN,           # add to .env
    traces_sample_rate=0.1,            # 10% of requests get performance traces
    send_default_pii=False,
)
```

No other changes needed — `sentry-sdk[fastapi]` auto-instruments FastAPI request/response cycles and captures any unhandled exception. Calls to `logger.exception(...)` are also forwarded to Sentry automatically because Sentry installs a `LoggingIntegration` by default (level=ERROR).

**Alerts:** configure Sentry to email or Slack you on the first occurrence of each new issue. That replaces manually tailing logs in production.

---

**Celery + Redis (background task queue)**

DELETE operations that trigger Google Calendar/Drive cleanup currently run synchronously — the HTTP response waits for cleanup to finish and returns any Google errors (`driveError`, `calendarError`) directly to the frontend. This is correct for one admin at low volume.

Not used because:
- **One admin, rare deletes.** Google cleanup failing once every few months; `logger.exception()` in stdout is sufficient visibility.
- **Inline warnings are better UX here.** The frontend shows an amber warning immediately if Google cleanup fails. Moving to async loses that feedback with no easy replacement.
- **Three processes instead of one.** Celery + Redis means running a web server, a worker process, and a Redis instance — permanently, in both local dev and production.

**When to add it:** multiple admins, high delete volume, or Google cleanup starts failing often enough that you need guaranteed retries with alerting rather than manual log inspection.

**How it works:**

```
DELETE /api/students/{id}
  → DB delete (fast, synchronous)
  → enqueue cleanup_google.delay(student_id)   ← hands off immediately
  → return 204

Celery worker (separate process):
  → picks up job from Redis
  → runs Google Calendar + Drive cleanup
  → if fails: retry in 60s → 5min → 30min (exponential backoff)
  → if exhausted: log + send admin email/Slack alert
```

**How to add it:**

```python
# requirements.txt
celery[redis]>=5.0
redis>=5.0

# app/celery.py
from celery import Celery
celery = Celery("tuition", broker="redis://localhost:6379/0")

# app/features/students/tasks.py
from app.celery import celery

@celery.task(bind=True, max_retries=3)
def cleanup_google_task(self, student_id: str):
    try:
        # Google Calendar + Drive cleanup logic here
        ...
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60)

# app/features/students/router.py — change delete endpoint
await service.delete_student_db_only(supabase, student_id)
cleanup_google_task.delay(student_id)
return Response(status_code=204)
```

**What you lose:** the inline `driveError` / `calendarError` amber warning in `StudentForm`. Replace it with a Sentry alert or admin email on task exhaustion — pair this change with Sentry so failures surface somewhere.

**To run locally:**
```bash
redis-server                        # terminal 1
uvicorn app.main:app --reload       # terminal 2
celery -A app.celery worker -l info # terminal 3
```

**When to upgrade beyond Celery + Redis:**

Celery + Redis handles one app doing background jobs — one sender, one receiver, tightly coupled. When the architecture grows beyond that, switch the broker:

- **RabbitMQ** — drop-in replacement for Redis as the Celery broker. Better routing control (exchange types, dead letter queues), more reliable message delivery guarantees. Switch when Redis feels too simple but you're still in a single-app world: `broker="amqp://localhost"` in `celery.py`, no other code changes.

- **SQS (AWS)** — managed message queue hosted by AWS. No Redis server to run or maintain — AWS handles availability and scaling. Good fit when already on AWS infrastructure. Still one sender, one receiver per message (like Redis), but cloud-managed. Use `celery[sqs]` as the broker.

- **Kafka** — for when multiple independent services need to react to the same event. Unlike Redis/SQS where a message is consumed and gone, Kafka keeps every message in a log for days — multiple consumer groups can each read independently, and you can rewind and reprocess past events. Switch when: you split into microservices (separate Google cleanup service, analytics service, billing service all subscribing to `StudentDeleted`), you need event replay, or you're processing millions of events per second. Significant operational complexity — partitions, consumer group offsets, schema registry. Most companies never need it.

The progression most teams follow: **Redis → RabbitMQ or SQS → Kafka**, only moving when the current tier's limitations are actually felt.

---

**Direct database connection — asyncpg + SQLAlchemy**

Currently all database access goes through the Supabase client (`app/shared/db.py`), which talks to PostgreSQL via Supabase's PostgREST HTTP API. Connection pooling is handled internally by the Supabase client — the singleton in `db.py` is intentional so the pool is created once and reused across all requests.

Not using asyncpg or SQLAlchemy directly because:
- **Supabase manages the pool.** No configuration needed — reusing one `AsyncClient` instance is sufficient.
- **PostgREST gives RLS and auth for free.** Connecting directly to PostgreSQL would require reimplementing row-level security enforcement manually.
- **Less infrastructure.** No connection string to manage, no pool size to tune, no migration tooling to set up.

**When to switch:** if you move away from Supabase to a self-managed PostgreSQL database (e.g. AWS RDS, Neon, Supabase self-hosted), you would connect directly to Postgres and manage the connection pool yourself.

The standard Python stack for this:

- **asyncpg** — the actual PostgreSQL driver. Fast, async, writes raw SQL. Use `asyncpg.create_pool()` once at startup and borrow connections per request.
- **SQLAlchemy** — sits on top of asyncpg. Lets you define tables as Python classes and query without writing SQL. Also handles database migrations via Alembic. Use when you have complex table relationships or want migration management.

```python
# asyncpg only — raw SQL, maximum control
pool = await asyncpg.create_pool("postgresql://user:pass@host/db", min_size=5, max_size=20)

async def get_db():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM students")

# SQLAlchemy — ORM, Python objects instead of SQL
engine = create_async_engine("postgresql+asyncpg://user:pass@host/db", pool_size=10)

async with AsyncSession(engine) as session:
    result = await session.execute(select(Student).where(Student.status == "Active"))
    students = result.scalars().all()
```

The lifespan context (see singleton note in `db.py`) becomes important here — the pool should be created on startup and explicitly closed on shutdown, not lazily initialised on the first request.

**The progression:** Supabase client (now) → asyncpg direct if you need raw performance or leave Supabase → SQLAlchemy on top of asyncpg if you want ORM + migrations.

---

**`response_model` and schema-first type generation**

FastAPI endpoints currently return raw Supabase data without a declared `response_model`. Adding `response_model` everywhere would mean maintaining a third type definition on top of `src/lib/types.ts` (frontend) and the Supabase schema — three places to update every time a column changes.

Not used because:
- **API is mostly a thin proxy.** Responses forward Supabase data largely unchanged — no transformation, no field stripping needed.
- **Triple maintenance at one-person scale.** DB schema + TypeScript types + Pydantic response models = three files to keep in sync manually.
- **One frontend, one developer.** TypeScript compilation catches shape mismatches on the frontend side — a second validation layer in FastAPI adds cost without proportional benefit.

**The one place it matters now:** any endpoint using `select("*")` — it returns every DB column including anything sensitive added in future. A `response_model` would automatically strip undeclared fields even if a new sensitive column is added tomorrow. Worth adding selectively on student endpoints as a security safeguard.

**When to add it fully:** multiple API consumers (second frontend, mobile app, third-party integration), multiple developers where the response shape needs to be a declared contract between teams, or any endpoint that touches sensitive fields.

**Current state — three places maintained manually:**

```
Supabase schema (DB columns)           ← actual source of truth today
    ↓  manual sync
app/shared/response_models.py          ← hand-written Pydantic models
    ↓  manual sync
src/lib/types.ts                       ← hand-written TypeScript types
```

Add a column → update all three files by hand. Miss one → silent mismatch between DB, API, and frontend.

**How the type maintenance problem is solved in industry — pick one source of truth:**

Rather than maintaining types in three places manually, industry picks one source of truth and generates everything else from it automatically:

```
One source of truth
        ↓
   code generation      ← run after every schema change
        ↓
Types in every language that needs them
```

There are two common approaches:

---

**Option A — DB schema as source of truth (fully automated)**

Generate both Pydantic models and TypeScript types from the live database schema. Nothing is hand-written except the schema migration.

```
Supabase schema
    ↓ supabase-pydantic (community tool)
app/shared/response_models.py          ← auto-generated, never hand-written
    ↓ FastAPI auto-generates
openapi.json
    ↓ openapi-typescript
src/lib/types.ts                       ← auto-generated, never hand-written
```

Add a column → run two codegen commands → both Python and TypeScript update automatically.

**Preferred when:** DB changes frequently, the DB schema IS the product design, and the team wants zero manual syncing. Common in data-heavy companies where DB architects drive the design.

**Hidden cost:** generated Pydantic models mirror every DB column including internal ones (`calendar_event_ids`, `created_at`). They expose your full DB shape to the API — a new sensitive column leaks unless you manually exclude it. You often end up maintaining a second layer of "clean" response models on top anyway, partially defeating the automation.

---

**Option B — Pydantic models as source of truth (partial automation)**

You hand-write the Pydantic `response_model` definitions. FastAPI generates an OpenAPI spec from them for free. A codegen tool generates TypeScript types from the spec — the frontend is never hand-written.

```
app/shared/response_models.py          ← you write and maintain this (one place)
    ↓ FastAPI auto-generates
openapi.json                           ← always up to date, free
    ↓ openapi-typescript
src/lib/types.ts                       ← auto-generated, never hand-written
```

Add a field → update `response_models.py` → run one command → `types.ts` updates automatically.

**Preferred when:** the API contract is the product — you control exactly what fields are exposed, with what types, independent of DB internals. Common in public API companies (Stripe, Twilio) where the API shape is versioned and guaranteed. Also preferred when the API and DB evolve independently.

**Advantage over Option A:** explicit control over what leaves the server. Sensitive DB columns are never accidentally exposed — only declared fields pass through.

---

**The most common real-world stack at mid-size companies:**

Neither extreme. Most teams use SQLAlchemy models as the single source of truth for both the DB and the API, sitting between Option A and B:

```
SQLAlchemy models + Alembic            ← one source of truth in Python code
    ↓ Alembic                          → DB migration SQL (DB derives from code)
    ↓ FastAPI response_model           → openapi.json (auto-generated)
    ↓ openapi-typescript               → src/lib/types.ts (auto-generated)
```

One Python model definition drives everything — DB schema, API validation, and frontend types. Change the model, run `alembic revision --autogenerate`, commit, done.

**This only applies when using a self-managed PostgreSQL** (AWS RDS, Neon, etc.) — not Supabase, which manages its own schema.

---

**Upgrade path for this project:**

```
Now (fully manual — current state):
  Supabase schema
    → hand-written response_models.py
    → hand-written src/lib/types.ts

Step 1 — eliminate frontend manual sync (Option B, one command):
  response_models.py (maintained manually — one place)
    → FastAPI auto-generates openapi.json
    → npx openapi-typescript http://localhost:8000/openapi.json -o src/lib/types.ts
  ← types.ts is never hand-written again; one command keeps it in sync

Step 2 — eliminate Pydantic manual sync (Option A, still on Supabase):
  Supabase schema
    → supabase-pydantic → response_models.py (auto-generated)
    → FastAPI → openapi.json → openapi-typescript → types.ts (auto-generated)
  ← zero manual type files; run two commands after every schema change

Step 3 — full pipeline on own managed DB:
  SQLAlchemy models + Alembic (one source of truth)
    → Alembic → DB migrations
    → FastAPI response_model → openapi.json → openapi-typescript → types.ts
  ← one model file, zero manual syncing across DB + backend + frontend
```

**Protobuf — the enterprise alternative:**

At large scale or when multiple services in different languages need to share the same data shapes, teams use **Protobuf** (Protocol Buffers) as the source of truth instead of OpenAPI. A single `.proto` file defines the data structure once; code generators produce client and server code for Python, TypeScript, Go, Java, and more.

```protobuf
// student.proto
message Student {
  string id = 1;
  string name = 2;
  string status = 3;
  float fee_per_hour = 4;
}
```

```bash
protoc --python_out=. --ts_out=. student.proto
# generates Python classes + TypeScript interfaces from the same file
```

Protobuf is paired with **gRPC** (Google's RPC framework) for service-to-service communication — faster than HTTP/JSON, strongly typed end-to-end, with generated client stubs in every language. Used by Google, Netflix, Uber for internal microservice communication. Overkill for a single-language HTTP API but worth knowing as the pattern behind large-scale type sharing.
