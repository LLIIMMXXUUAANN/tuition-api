"""Post-tool hooks for self-evaluation after mutations.

Port of src/features/agent/lib/lg/post-hooks.ts.
"""

from __future__ import annotations

import asyncio
import json

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from app.features.agent.eval import self_eval
from app.features.agent.lg.agent_state import AgentState

STUDENT_MUTATIONS = {
    "create_student",
    "update_student",
    "delete_student",
    "manage_portal_access",
}
TIMETABLE_MUTATIONS = {
    "update_timetable_rules",
    "update_buffer_mins",
}


def _find_round_mutation_calls(messages: list, mutation_set: set) -> list[dict]:
    """Find mutation tool calls from the most recent tool-calling AIMessage."""
    for m in reversed(messages):
        if isinstance(m, (AIMessage, AIMessageChunk)):
            tool_calls = getattr(m, "tool_calls", None) or []
            if tool_calls:
                return [
                    {"name": tc["name"], "args": tc.get("args") or {}, "tool_call_id": tc.get("id")}
                    for tc in tool_calls
                    if tc.get("name") in mutation_set
                ]
    return []


def _find_created_id(messages: list, tool_call_id: str | None) -> str | None:
    """Find the student ID returned by a create_student tool call."""
    if not tool_call_id:
        return None
    for m in messages:
        if not isinstance(m, ToolMessage) or m.tool_call_id != tool_call_id:
            continue
        content = m.content if isinstance(m.content, str) else json.dumps(m.content)
        try:
            parsed = json.loads(content)
            return parsed.get("id")
        except Exception:
            return None
    return None


def _make_post_hook(supabase, mutation_set: set):
    """Return an async post-hook function for the given mutation set."""

    async def post_hook(state: AgentState) -> dict:
        messages = state["messages"]
        mutations = _find_round_mutation_calls(messages, mutation_set)
        if not mutations:
            return {}

        verdicts = await asyncio.gather(*[
            self_eval(
                m["name"], m["args"], supabase,
                _find_created_id(messages, m.get("tool_call_id")) if m["name"] == "create_student" else None,
            )
            for m in mutations
        ])
        combined = "  \n".join(v for v in verdicts if v)
        if not combined:
            return {}
        return {"audit_log": [combined]}

    return post_hook


def make_student_post_hook(supabase):
    """Return a post-hook that verifies student mutations."""
    return _make_post_hook(supabase, STUDENT_MUTATIONS)


def make_timetable_post_hook(supabase):
    """Return a post-hook that verifies timetable mutations."""
    return _make_post_hook(supabase, TIMETABLE_MUTATIONS)
