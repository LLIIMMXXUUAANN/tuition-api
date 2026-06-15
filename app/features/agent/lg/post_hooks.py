"""Post-tool hooks for self-evaluation after mutations.

Port of src/features/agent/lib/lg/post-hooks.ts.
"""

from __future__ import annotations

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


def _find_last_mutation_call(messages: list, mutation_set: set) -> dict | None:
    """Find the last mutation tool call, only searching after the last self_eval message
    to avoid re-verifying prior mutations when the subagent makes more calls."""
    search_from = len(messages) - 1

    # Find the last self_eval SystemMessage to limit the search window
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, SystemMessage) and getattr(m, "name", None) == SELF_EVAL_MESSAGE_NAME:
            search_from = i - 1
            break

    for i in range(search_from, -1, -1):
        m = messages[i]
        if not isinstance(m, (AIMessage, AIMessageChunk)):
            continue
        tool_calls = getattr(m, "tool_calls", None) or []
        call = next((tc for tc in tool_calls if tc.get("name") in mutation_set), None)
        if call:
            return {
                "name": call["name"],
                "args": call.get("args") or {},
                "tool_call_id": call.get("id"),
            }
    return None


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
            return parsed.get("student", {}).get("id")
        except Exception:
            return None
    return None


def _make_post_hook(supabase, mutation_set: set):
    """Return an async post-hook function for the given mutation set."""

    async def post_hook(state: MessagesState) -> dict:
        messages = state["messages"]
        last = _find_last_mutation_call(messages, mutation_set)
        if not last:
            return {}

        created_id = None
        if last["name"] == "create_student":
            created_id = _find_created_id(messages, last.get("tool_call_id"))

        verdict = await self_eval(last["name"], last["args"], supabase, created_id)
        if not verdict:
            return {}

        return {
            "messages": [
                SystemMessage(content=verdict, name=SELF_EVAL_MESSAGE_NAME)
            ]
        }

    return post_hook


def make_student_post_hook(supabase):
    """Return a post-hook that verifies student mutations."""
    return _make_post_hook(supabase, STUDENT_MUTATIONS)


def make_timetable_post_hook(supabase):
    """Return a post-hook that verifies timetable mutations."""
    return _make_post_hook(supabase, TIMETABLE_MUTATIONS)
