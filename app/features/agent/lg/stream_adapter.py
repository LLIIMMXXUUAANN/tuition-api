"""LangGraph stream adapter — port of src/features/agent/lib/lg/stream-adapter.ts.

Translates LangGraph's multi-mode event stream into the same SSE event types
as the classic agent: chunk, step, done, stopped, error, download_schedule, slots_ready.
"""

from __future__ import annotations

import json
from typing import AsyncGenerator, AsyncIterable, Callable, Optional

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.features.agent.state import stop_signals

SELF_EVAL_NAME = "self_eval"


# ---------------------------------------------------------------------------
# Namespace helpers
# ---------------------------------------------------------------------------


def _is_from_supervisor(namespace) -> bool:
    """True for events from the outer supervisor graph (empty or 'supervisor:' prefix)."""
    if namespace is None or len(namespace) == 0:
        return True
    return str(namespace[0]).startswith("supervisor:")


def _is_from_any_subagent(namespace) -> bool:
    """True for events from any subagent namespace."""
    if namespace is None or len(namespace) == 0:
        return False
    return not str(namespace[0]).startswith("supervisor:")


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def _extract_text(msg) -> str:
    """Extract plain text from an AIMessage / AIMessageChunk."""
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


# ---------------------------------------------------------------------------
# Tool step emission
# ---------------------------------------------------------------------------


def _emit_tool_steps(messages: list) -> list[dict]:
    """Return SSE step events for real tool calls (excluding routing messages)."""
    events: list[dict] = []
    for m in messages:
        if isinstance(m, (AIMessage, AIMessageChunk)) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                name = tc.get("name", "")
                if name == "dispatch" or name.startswith("transfer_back_to_"):
                    continue
                args_str = json.dumps(tc.get("args") or {})
                events.append({"data": json.dumps({"type": "step", "content": f"\U0001f527 {name}({args_str})"})})
        elif isinstance(m, SystemMessage) and getattr(m, "name", None) == SELF_EVAL_NAME:
            text = m.content if isinstance(m.content, str) else json.dumps(m.content)
            events.append({"data": json.dumps({"type": "step", "content": text})})
    return events


# ---------------------------------------------------------------------------
# Routing-relevance filter
# ---------------------------------------------------------------------------


def is_routing_relevant(msg: BaseMessage) -> bool:
    """Return True for messages that belong in lgHistory (routing-level context only)."""
    if isinstance(msg, HumanMessage):
        return True
    if isinstance(msg, SystemMessage):
        return getattr(msg, "name", None) == SELF_EVAL_NAME
    if isinstance(msg, ToolMessage):
        name = getattr(msg, "name", "") or ""
        return name == "dispatch" or name.startswith("transfer_back_to_")
    if isinstance(msg, (AIMessage, AIMessageChunk)):
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            return True  # direct reply (no tool calls) — keep
        return any(
            tc.get("name") == "dispatch" or (tc.get("name") or "").startswith("transfer_back_to_")
            for tc in tool_calls
        )
    return False


# ---------------------------------------------------------------------------
# Main stream adapter
# ---------------------------------------------------------------------------


async def pipe_langgraph_stream(
    stream: AsyncIterable,
    request_id: Optional[str] = None,
    on_complete: Optional[Callable] = None,
) -> AsyncGenerator[dict, None]:
    """Async generator that translates a LangGraph stream into SSE event dicts.

    Each yielded dict has the shape {"data": json_string} compatible with sse-starlette.
    """
    streamed_any_text = False
    last_supervisor_final_text = ""
    accumulated_map: dict[str, BaseMessage] = {}

    async for raw in stream:
        # Events arrive as (namespace, mode, data) or (mode, data) tuples
        if not isinstance(raw, tuple):
            continue

        if len(raw) == 3:
            namespace, mode, data = raw
        elif len(raw) == 2:
            namespace = None
            mode, data = raw
        else:
            continue

        if mode == "messages":
            # Streaming text chunks from the supervisor
            chunk = data[0] if isinstance(data, tuple) else data
            if not isinstance(chunk, (AIMessage, AIMessageChunk)):
                continue
            # Skip chunks containing tool calls
            if getattr(chunk, "tool_calls", None) or getattr(chunk, "tool_call_chunks", None):
                continue
            if not _is_from_supervisor(namespace) or _is_from_any_subagent(namespace):
                continue
            text = _extract_text(chunk)
            if not text:
                continue
            yield {"data": json.dumps({"type": "chunk", "content": text})}
            streamed_any_text = True

        elif mode == "updates":
            if not isinstance(data, dict):
                continue
            for node_output in data.values():
                if not isinstance(node_output, dict):
                    continue
                msgs = node_output.get("messages", [])
                if not isinstance(msgs, list):
                    continue

                # Emit step events for tool calls in this node's output
                for step_event in _emit_tool_steps(msgs):
                    yield step_event

                # Accumulate all messages for history
                for m in msgs:
                    msg_id = getattr(m, "id", None)
                    if msg_id:
                        accumulated_map[msg_id] = m

                # Capture final supervisor text for fallback
                if _is_from_supervisor(namespace) and not _is_from_any_subagent(namespace):
                    for m in msgs:
                        if isinstance(m, (AIMessage, AIMessageChunk)) and not getattr(m, "tool_calls", None):
                            text = _extract_text(m)
                            if text:
                                last_supervisor_final_text = text

        elif mode == "custom":
            if isinstance(data, dict):
                if "download_schedule" in data:
                    yield {"data": json.dumps({"type": "download_schedule", "students": data["download_schedule"]})}
                if "slots_ready" in data:
                    yield {"data": json.dumps({"type": "slots_ready", "slots": data["slots_ready"]})}

        # Belt-and-suspenders stop check after each event
        if request_id and stop_signals.get(request_id, False):
            stop_signals.pop(request_id, None)
            yield {"data": json.dumps({"type": "stopped"})}
            return

    # Fallback: if supervisor never streamed text, emit the captured final text
    if not streamed_any_text:
        fallback = last_supervisor_final_text or "(no response from supervisor — check server logs)"
        yield {"data": json.dumps({"type": "chunk", "content": fallback})}

    # Call the on_complete callback (emits lg_history) before done
    accumulated = list(accumulated_map.values())
    if on_complete:
        async for event in on_complete(accumulated):
            yield event

    yield {"data": json.dumps({"type": "done"})}
