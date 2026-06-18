"""Custom supervisor + multi-agent graph — port of src/features/agent/lib/lg/custom-supervisor.ts
and src/features/agent/lib/lg/supervisor.ts.
"""

from __future__ import annotations

import uuid

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph

try:
    from langgraph.types import Command, Send
except ImportError:
    from langgraph.graph import Command, Send

from app.features.agent.lg.handoff import create_dispatch_tool, normalize_agent_name
from app.features.agent.lg.model import get_gemini_chat_model
from app.features.agent.lg.student_agent import make_student_agent
from app.features.agent.lg.template_agent import make_template_agent
from app.features.agent.lg.timetable_agent import make_timetable_agent


# ---------------------------------------------------------------------------
# Handoff message helpers
# ---------------------------------------------------------------------------


def _extract_text(msg) -> str:
    """Extract plain text content from an AIMessage / AIMessageChunk."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "".join(parts)
    return ""


def _create_handoff_back_messages(
    agent_name: str, supervisor_name: str, reply_text: str
) -> list:
    """Synthesise an AIMessage + ToolMessage pair for the subagent's reply.

    The ToolMessage content carries the actual subagent reply so Gemini echoes
    the correct answer rather than a generic stub.
    """
    tool_call_id = str(uuid.uuid4())
    tool_name = f"transfer_back_to_{normalize_agent_name(supervisor_name)}"
    return [
        AIMessage(
            content="",
            tool_calls=[{"name": tool_name, "args": {}, "id": tool_call_id}],
            name=agent_name,
        ),
        ToolMessage(
            content=reply_text or f"Subagent {agent_name} completed its task.",
            name=tool_name,
            tool_call_id=tool_call_id,
        ),
    ]


def make_call_agent(agent, supervisor_name: str):
    """Wrap a compiled subagent so it returns handoff-back messages for the supervisor."""

    async def call_agent(state: MessagesState, config: RunnableConfig = None) -> dict:
        output = await agent.ainvoke(state, config=config)
        messages = output.get("messages", [])

        # Find last AI reply without tool calls
        reply_text = ""
        for m in reversed(messages):
            if isinstance(m, (AIMessage, AIMessageChunk)) and not getattr(m, "tool_calls", None):
                reply_text = _extract_text(m)
                break

        return {"messages": _create_handoff_back_messages(agent.name, supervisor_name, reply_text)}

    return call_agent


# ---------------------------------------------------------------------------
# Supervisor prompt
# ---------------------------------------------------------------------------


def build_supervisor_prompt(date_string: str) -> str:
    return f"""\
Today is {date_string} (Malaysia Time).

You are the routing supervisor for a multi-agent tuition admin system. You have three specialist subagents:

- **student_agent** — student records (CRUD, portal access emails, day-of-week schedules, monthly fee summaries)
- **template_agent** — message templates (payment, review, recommendation, first-approach) and generated personalised payment messages
- **timetable_agent** — timetable scheduling rules, buffer minutes, slot-availability generation, and downloading the weekly schedule PNG

DECISION RULES:

**Answer the user directly (do NOT route)** for any of these:
- Greetings and small talk: "hi", "hello", "thanks", "good morning", "how are you"
- Meta-questions about yourself or the system: "what can you do", "what subagents do you have", "how do you work"
- Capability questions: "can you delete a student?" → answer "Yes, just tell me who" — do NOT actually delete
- Off-topic refusals: "what's the weather?", "who won the election?" → reply briefly "I help with students, templates, and timetable. What can I help with?"
Keep these direct replies to 1–2 short sentences.

**Route to a subagent (do NOT answer directly)** for anything else:
- Any request that names specific data (student names, fees, dates, templates by id)
- Any look-up, create, update, delete, download, sync, or generate action
- Any question whose answer would change if the database changes ("how many students do I have?", "who's on Tuesday?" — these are data, route them)

ROUTING:
You have ONE routing tool: `dispatch`. Call it with an array of `{{ agentName, task }}` entries.
- **Same agent, multiple entities** → ONE entry with a combined task. The subagent handles all entities in one invocation and batches tool calls in parallel internally. Do NOT create separate entries for the same agent.
- **Different agents** (cross-domain, truly parallel) → multiple entries in ONE `dispatch` call — they run at the same time
- **Sequential tasks** (one's output feeds the next) → call `dispatch` once for the first; the subagent replies in the next supervisor turn; then call `dispatch` again with the second task using that reply

Examples:
- "show me details for Ang and Zng Yi" → `dispatch({{ handoffs: [{{ agentName: "student_agent", task: "Get full details for both Ang and Zng Yi." }}] }})` (one entry — same agent)
- "list students AND show first-approach template" → `dispatch({{ handoffs: [{{ agentName: "student_agent", task: "List all active students." }}, {{ agentName: "template_agent", task: "Get the first-approach template." }}] }})` (two entries — different agents)

❌ WRONG — two entries for same agent (spawns two separate subagent instances):
  `dispatch({{ handoffs: [{{ agentName: "student_agent", task: "Update Ang fee to 60." }}, {{ agentName: "student_agent", task: "Update Zng Yi fee to 60." }}] }})`

✓ RIGHT — one combined entry (subagent batches the tool calls internally):
  `dispatch({{ handoffs: [{{ agentName: "student_agent", task: "Update both Ang and Zng Yi fee to 60." }}] }})`
- **Payment messages always require a student UUID.** If the user names a student (not a UUID), first dispatch to student_agent to get the UUID, then in a second dispatch call route to template_agent with the UUID in the task.

WRITING TASKS FOR SUBAGENTS:
Always write a precise, self-contained task in the `task` field:
- Resolve time references using today's injected date: "today" → specific date, "this month" → "May 2026"
- State the exact action: "Get...", "Create...", "Update...", "Generate..."
- Each task must stand alone — subagents cannot see each other's tasks or the conversation history
- **Never expand or guess student names** — copy the exact name or partial name the user typed (search_students does partial matching, so "Ang" is a valid search term)
- **UUID propagation (important):** Scan the conversation history for [student_id:NAME:UUID] tokens in prior replies. If a student's UUID is already known, include it explicitly in the task so the subagent can act directly without searching. Format: "Update Ang (id: 2dfa867c-b2b8-472d-96a5-63f4c2d5e466) fee to 60." If the UUID is NOT known, use only the name — the subagent will search.
- Example: `{{ agentName: "student_agent", task: "Get the class schedule for Tuesday 2026-05-19." }}`

RELAYING SUBAGENT REPLIES:
When a subagent calls transfer_back_to_supervisor, its reply is in the content of that ToolMessage — output it VERBATIM as your final answer.
- NEVER output "Successfully transferred back to supervisor" or "Transferring back to supervisor" — those are internal routing signals, not user-facing replies.
- Do NOT rephrase, summarise, or add any commentary.
- Do NOT remove [student_id:NAME:UUID] tokens or download-button hints — the UI depends on them.
- If multiple subagents replied (parallel handoff), concatenate their replies in the order they were requested, separated by one blank line. No headings between them.

CRITICAL — every supervisor turn MUST produce one of:
1. A `dispatch` tool call (routing to one or more subagents)
2. A non-empty text response (direct answer or verbatim relay of subagent output)
Empty output — no text and no tool call — is NEVER valid. If you find yourself about to produce nothing, output the last ToolMessage content verbatim instead.\
"""


# ---------------------------------------------------------------------------
# Custom supervisor builder
# ---------------------------------------------------------------------------


def build_custom_supervisor(agents: list, llm, prompt: str, supervisor_name: str = "supervisor"):
    """Build the supervisor + subagent multi-agent graph.

    Replaces @langchain/langgraph-supervisor to fix:
    1. The official package echoes the handoff ToolMessage content instead of the subagent's reply.
    2. createReactAgent always makes two LLM calls per supervisor turn — wasted for a routing supervisor.
    """
    agent_names = [a.name for a in agents]
    agent_info = [{"name": a.name, "description": getattr(a, "description", "")} for a in agents]

    dispatch_tool = create_dispatch_tool(agent_info)
    supervisor_llm = llm.bind_tools([dispatch_tool])

    async def supervisor_node(state: MessagesState, config: RunnableConfig = None):
        input_msgs = [SystemMessage(content=prompt)] + state["messages"]

        accumulated = None
        async for chunk in supervisor_llm.astream(input_msgs, config=config):
            if accumulated is None:
                accumulated = chunk
            else:
                accumulated = accumulated + chunk

        if accumulated is None:
            raise RuntimeError("Supervisor LLM returned empty response")

        # Convert AIMessageChunk to AIMessage for stable serialization
        response = AIMessage(
            content=accumulated.content,
            tool_calls=list(accumulated.tool_calls or []),
            id=accumulated.id,
            additional_kwargs=dict(accumulated.additional_kwargs or {}),
        )

        dispatch_call = next(
            (tc for tc in (response.tool_calls or []) if tc.get("name") == "dispatch"),
            None,
        )

        if not dispatch_call:
            text = _extract_text(response)
            if not text:
                # LLM went silent — deterministically relay last subagent replies
                last_dispatch_idx = -1
                for i, m in enumerate(state["messages"]):
                    if isinstance(m, ToolMessage) and getattr(m, "name", "") == "dispatch":
                        last_dispatch_idx = i
                relay_parts = []
                for m in state["messages"][last_dispatch_idx + 1 :]:
                    if isinstance(m, ToolMessage) and (getattr(m, "name", "") or "").startswith("transfer_back_to_"):
                        content = m.content if isinstance(m.content, str) else ""
                        if content:
                            relay_parts.append(content)
                if relay_parts:
                    text = "\n\n".join(relay_parts)
            return {"messages": [AIMessage(content=text or "", id=response.id)]}

        handoffs = dispatch_call["args"].get("handoffs", [])
        # handoffs may be dicts or Pydantic objects
        handoff_list = []
        for h in handoffs:
            if isinstance(h, dict):
                handoff_list.append(h)
            else:
                # Pydantic model
                handoff_list.append({"agentName": h.agentName, "task": h.task})

        # Merge entries targeting the same agent into one combined task.
        # dict preserves insertion order (Python 3.7+) so dispatch order is stable.
        merged: dict[str, str] = {}
        for h in handoff_list:
            agent = h["agentName"]
            if agent in merged:
                merged[agent] = merged[agent] + "\n" + h["task"]
            else:
                merged[agent] = h["task"]
        merged_list = [{"agentName": k, "task": v} for k, v in merged.items()]

        tool_msg = ToolMessage(
            content=f"Dispatching {len(merged_list)} task(s) to: {', '.join(h['agentName'] for h in merged_list)}",
            name="dispatch",
            tool_call_id=dispatch_call.get("id") or "",
        )

        return Command(
            update={"messages": [response, tool_msg]},
            goto=[
                Send(h["agentName"], {"messages": [HumanMessage(h["task"])]})
                for h in merged_list
            ],
        )

    builder = StateGraph(MessagesState)
    builder.add_node(supervisor_name, supervisor_node, destinations=agent_names)
    builder.add_edge(START, supervisor_name)
    builder.add_edge(supervisor_name, END)

    for agent in agents:
        builder.add_node(agent.name, make_call_agent(agent, supervisor_name))
        builder.add_edge(agent.name, supervisor_name)

    compiled = builder.compile()
    compiled.name = "supervisor"
    return compiled


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def make_supervisor(supabase, date_string: str):
    """Create and compile the full supervisor + three subagents graph."""
    student_agent = make_student_agent(supabase)
    template_agent = make_template_agent(supabase)
    timetable_agent = make_timetable_agent(supabase)

    agents = [student_agent, template_agent, timetable_agent]
    prompt = build_supervisor_prompt(date_string)
    llm = get_gemini_chat_model()

    return build_custom_supervisor(agents, llm, prompt, supervisor_name="supervisor")
