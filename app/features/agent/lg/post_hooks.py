"""Post-tool hooks for self-evaluation after mutations.

Port of src/features/agent/lib/lg/post-hooks.ts.
"""

from __future__ import annotations

import asyncio
import json

from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage, ToolMessage
from langgraph.graph import MessagesState

from app.features.agent.eval import self_eval

SELF_EVAL_MESSAGE_NAME = "self_eval"

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
    """Find ALL mutation tool calls from the most recent tool-calling round,
    only searching messages AFTER the last self_eval message."""
    last_self_eval_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, SystemMessage) and getattr(m, "name", None) == SELF_EVAL_MESSAGE_NAME:
            last_self_eval_idx = i
            break

    # range stops before last_self_eval_idx so we never re-examine prior rounds
    for i in range(len(messages) - 1, last_self_eval_idx, -1):
        m = messages[i]
        if not isinstance(m, (AIMessage, AIMessageChunk)):
            continue
        tool_calls = getattr(m, "tool_calls", None) or []
        mutations = [tc for tc in tool_calls if tc.get("name") in mutation_set]
        if mutations:
            return [
                {"name": tc["name"], "args": tc.get("args") or {}, "tool_call_id": tc.get("id")}
                for tc in mutations
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

    async def post_hook(state: MessagesState) -> dict:
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
        return {"messages": [SystemMessage(content=combined, name=SELF_EVAL_MESSAGE_NAME)]}

    return post_hook


def make_student_post_hook(supabase):
    """Return a post-hook that verifies student mutations."""
    return _make_post_hook(supabase, STUDENT_MUTATIONS)


def make_timetable_post_hook(supabase):
    """Return a post-hook that verifies timetable mutations."""
    return _make_post_hook(supabase, TIMETABLE_MUTATIONS)
