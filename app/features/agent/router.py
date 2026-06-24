"""Agent SSE streaming endpoints — port of src/app/api/agent/chat/route.ts and stop/route.ts.

Also includes /lg/chat endpoint (LangGraph multi-agent) — port of src/app/api/agent/lg/chat/route.ts.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import time

import pytz
from fastapi import APIRouter, Depends, Request
from google.genai import types
from langchain_core.messages import (
    AIMessage as LCAIMessage,
    HumanMessage as LCHumanMessage,
    messages_from_dict,
    messages_to_dict,
)
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.features.agent.eval import self_eval
from app.features.agent.lg.stream_adapter import is_routing_relevant, pipe_langgraph_stream
from app.features.agent.lg.supervisor import make_supervisor
from app.features.agent.schema import SYSTEM_INSTRUCTION, TOOL_DECLARATIONS
from app.features.agent.state import stop_signals
from app.features.agent.utils import TRAILING_BUFFER, extract_student_tokens
from app.shared.schema import CamelResponse
from app.features.agent.tools import (
    create_student,
    delete_student,
    download_timetable_image,
    generate_payment_message,
    generate_slot_availability,
    get_fee_summary,
    get_schedule,
    get_student,
    get_template,
    get_timetable_settings,
    list_students,
    list_templates,
    manage_portal_access,
    run_sync_all,
    search_students,
    update_buffer_mins,
    update_student,
    update_timetable_rules,
)
from app.auth import require_internal_secret
from app.shared.gemini.client import gemini_client
from app.shared.db import get_supabase

router = APIRouter(dependencies=[Depends(require_internal_secret)], default_response_class=CamelResponse)

MUTATION_TOOLS = {
    "update_student",
    "delete_student",
    "update_timetable_rules",
    "update_buffer_mins",
    "manage_portal_access",
}


# ---------------------------------------------------------------------------
# History conversion helpers
# ---------------------------------------------------------------------------


def _to_content(raw: dict) -> types.Content:
    """Convert TypeScript-serialized Content dict to Python SDK Content."""
    role = raw["role"]
    parts: list[types.Part] = []
    for p in raw.get("parts", []):
        if p.get("text"):
            parts.append(types.Part(text=p["text"]))
        elif "functionCall" in p:
            fc = p["functionCall"]
            parts.append(types.Part(
                function_call=types.FunctionCall(
                    name=fc["name"],
                    args=fc.get("args") or {},
                )
            ))
        elif "functionResponse" in p:
            fr = p["functionResponse"]
            parts.append(types.Part(
                function_response=types.FunctionResponse(
                    name=fr["name"],
                    response=fr.get("response") or {},
                )
            ))
    return types.Content(role=role, parts=parts)


def _content_to_dict(c: types.Content) -> dict:
    """Serialize a Content object to TypeScript-compatible JSON dict."""
    parts: list[dict] = []
    for p in (c.parts or []):
        if hasattr(p, "text") and p.text:
            parts.append({"text": p.text})
        elif hasattr(p, "function_call") and p.function_call:
            fc = p.function_call
            parts.append({"functionCall": {
                "name": fc.name,
                "args": dict(fc.args) if fc.args else {},
            }})
        elif hasattr(p, "function_response") and p.function_response:
            fr = p.function_response
            parts.append({"functionResponse": {
                "name": fr.name,
                "response": dict(fr.response) if fr.response else {},
            }})
    return {"role": c.role, "parts": parts}


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------


async def execute_tool(
    name: str, args: dict, supabase, side_effects: list[dict] | None = None
) -> dict | list:
    match name:
        case "search_students":
            return await search_students(supabase, args["query"])
        case "get_student":
            return await get_student(supabase, args["id"])
        case "list_students":
            return await list_students(supabase, args)
        case "create_student":
            return await create_student(supabase, args)
        case "update_student":
            return await update_student(supabase, args["id"], args["fields"])
        case "delete_student":
            return await delete_student(supabase, args["id"])
        case "sync_all_students":
            return await run_sync_all(supabase)
        case "manage_portal_access":
            return await manage_portal_access(supabase, args["student_id"], args["action"], args["email"])
        case "get_schedule":
            return await get_schedule(supabase, args["day"])
        case "get_fee_summary":
            return await get_fee_summary(supabase, args.get("month"), args.get("year"))
        case "list_templates":
            return list_templates()
        case "get_template":
            return await get_template(supabase, args["id"])
        case "generate_payment_message":
            return await generate_payment_message(supabase, args)
        case "get_timetable_settings":
            return await get_timetable_settings(supabase)
        case "update_timetable_rules":
            return await update_timetable_rules(supabase, args["rules"])
        case "update_buffer_mins":
            return await update_buffer_mins(supabase, args["buffer_mins"])
        case "generate_slot_availability":
            result = await generate_slot_availability(supabase, args.get("student_availability", ""))
            if side_effects is not None and isinstance(result, dict) and "slots" in result:
                side_effects.append({"type": "ui_action", "action": "slots_ready", "payload": {"slots": result["slots"]}})
            return result
        case "download_timetable_image":
            result = await download_timetable_image(supabase)
            if side_effects is not None and isinstance(result, dict) and "students" in result:
                side_effects.append({"type": "ui_action", "action": "download_schedule", "payload": {"students": result["students"]}})
            return result
        case _:
            return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Chat endpoint
# ---------------------------------------------------------------------------


@router.post("/chat")
async def agent_chat(request: Request):
    body = await request.json()
    messages: list[dict] = body.get("messages", [])
    request_id: str | None = body.get("requestId")
    gemini_history: list[dict] = body.get("geminiHistory", [])

    if not messages:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "messages is required"}, status_code=400)

    # Build MYT date string (cross-platform: no %-d)
    MYT = pytz.timezone("Asia/Kuala_Lumpur")
    now_myt = datetime.datetime.now(tz=MYT)
    myt_date_str = now_myt.strftime("%A, %B ") + str(now_myt.day) + now_myt.strftime(", %Y")
    system_instruction = f"Today is {myt_date_str} (Malaysia Time).\n\n{SYSTEM_INSTRUCTION}"

    supabase = await get_supabase()

    async def event_gen():
        # Build initial contents
        contents: list[types.Content]
        if gemini_history:
            latest_msg = messages[-1]["content"]
            contents = [_to_content(c) for c in gemini_history]
            contents.append(types.Content(role="user", parts=[types.Part(text=latest_msg)]))
        else:
            contents = []
            for m in messages:
                role = "model" if m["role"] == "agent" else m["role"]
                contents.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))

        got_reply = False

        try:
            for _round in range(10):
                if stop_signals.get(request_id, False):
                    break

                round_text = ""
                round_fn_calls: list = []
                trailing = ""

                async for chunk in await gemini_client.aio.models.generate_content_stream(
                    model="gemini-2.5-flash",
                    contents=contents,
                    config=types.GenerateContentConfig(
                        tools=TOOL_DECLARATIONS,
                        system_instruction=system_instruction,
                        temperature=0,
                    ),
                ):
                    parts = []
                    if chunk.candidates and chunk.candidates[0].content:
                        parts = chunk.candidates[0].content.parts or []
                    for part in parts:
                        if hasattr(part, "function_call") and part.function_call:
                            round_fn_calls.append(part.function_call)
                        elif hasattr(part, "text") and part.text:
                            round_text += part.text
                            if len(round_fn_calls) == 0:
                                trailing += part.text
                                if len(trailing) > TRAILING_BUFFER:
                                    flush, trailing = trailing[:-TRAILING_BUFFER], trailing[-TRAILING_BUFFER:]
                                    yield {"data": json.dumps({"type": "chunk", "content": flush})}

                # Rebuild model content from accumulated round
                model_parts: list[types.Part] = []
                if round_text:
                    model_parts.append(types.Part(text=round_text))
                for fc in round_fn_calls:
                    model_parts.append(types.Part(
                        function_call=types.FunctionCall(
                            name=fc.name,
                            args=dict(fc.args or {}),
                        )
                    ))
                if model_parts:
                    contents.append(types.Content(role="model", parts=model_parts))

                if not round_fn_calls:
                    got_reply = True
                    cleaned, students = extract_student_tokens(trailing)
                    if students:
                        yield {"data": json.dumps({"type": "ui_action", "action": "student_links", "payload": {"studentLinks": students}})}
                    if cleaned:
                        yield {"data": json.dumps({"type": "chunk", "content": cleaned})}
                    break

                named_calls = [fc for fc in round_fn_calls if fc.name]

                # Emit step events before parallel execution (stable display order)
                for fc in named_calls:
                    yield {"data": json.dumps({"type": "step", "content": f"🔧 {fc.name}({json.dumps(dict(fc.args or {}))})" })}

                # Execute all tools in parallel, record timings
                timings: list[dict] = [{}] * len(named_calls)
                side_effects: list[dict] = []

                async def run_tool(fc, idx: int):
                    t0 = time.monotonic()
                    result = await execute_tool(fc.name, dict(fc.args or {}), supabase, side_effects)
                    timings[idx] = {"name": fc.name, "ms": int((time.monotonic() - t0) * 1000)}
                    return result

                round_start = time.monotonic()
                tool_results = await asyncio.gather(*[run_tool(fc, i) for i, fc in enumerate(named_calls)])

                if len(named_calls) > 1:
                    round_ms = int((time.monotonic() - round_start) * 1000)
                    timing_str = ", ".join(f"{t['name']} {t['ms']}ms" for t in timings)
                    yield {"data": json.dumps({"type": "step", "content": f"⏱ parallel ×{len(named_calls)} — {timing_str} (total {round_ms}ms)"})}

                # Build function response parts
                fn_response_parts: list[types.Part] = []
                for i, fc in enumerate(named_calls):
                    result = tool_results[i]
                    fn_response_parts.append(types.Part(
                        function_response=types.FunctionResponse(
                            name=fc.name,
                            response={"result": result},
                        )
                    ))

                # Emit UI side-effect events collected during tool execution
                for event in side_effects:
                    yield {"data": json.dumps(event)}

                contents.append(types.Content(role="user", parts=fn_response_parts))

                # Verify all mutations from this round in parallel
                round_mutations = []
                for i, fc in enumerate(named_calls):
                    result = tool_results[i]
                    if fc.name == "create_student" and isinstance(result, dict) and result.get("id"):
                        round_mutations.append({"name": fc.name, "args": dict(fc.args or {}), "created_id": result["id"]})
                    elif fc.name in MUTATION_TOOLS:
                        round_mutations.append({"name": fc.name, "args": dict(fc.args or {})})
                if round_mutations:
                    verdicts = await asyncio.gather(*[
                        self_eval(m["name"], m["args"], supabase, m.get("created_id"))
                        for m in round_mutations
                    ])
                    for verdict in verdicts:
                        if verdict:
                            yield {"data": json.dumps({"type": "step", "content": verdict})}

        except Exception as e:
            if request_id:
                stop_signals.pop(request_id, None)
            yield {"data": json.dumps({"type": "error", "message": str(e)})}
            return

        was_stopped = bool(stop_signals.pop(request_id, False)) if request_id else False

        if not got_reply and not was_stopped:
            yield {"data": json.dumps({"type": "chunk", "content": "I wasn't able to complete that in the allowed steps — please try a simpler request."})}

        if not was_stopped:
            history_dicts = [_content_to_dict(c) for c in contents]
            yield {"data": json.dumps({"type": "history", "contents": history_dicts})}

        yield {"data": json.dumps({"type": "stopped" if was_stopped else "done"})}

    return EventSourceResponse(event_gen())


# ---------------------------------------------------------------------------
# Stop endpoint
# ---------------------------------------------------------------------------


class StopRequest(BaseModel):
    requestId: str | None = None


@router.post("/stop")
async def stop_agent(body: StopRequest):
    if body.requestId:
        stop_signals[body.requestId] = True
    return {"ok": True}


# ---------------------------------------------------------------------------
# LangGraph multi-agent chat endpoint
# ---------------------------------------------------------------------------


@router.post("/lg/chat")
async def lg_chat(request: Request):
    body = await request.json()
    messages_raw: list[dict] = body.get("messages", [])
    request_id: str | None = body.get("requestId")
    lg_history: list[dict] = body.get("lgHistory", [])

    if not messages_raw:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "messages is required"}, status_code=400)

    # Build message history: restore from stored LangGraph history or convert from text
    if lg_history:
        latest_user_msg = messages_raw[-1]["content"]
        restored = messages_from_dict(lg_history)
        messages = restored + [LCHumanMessage(latest_user_msg)]
    else:
        messages = [
            LCAIMessage(m["content"]) if m["role"] == "model" else LCHumanMessage(m["content"])
            for m in messages_raw
        ]

    # MYT date string (cross-platform: no %-d)
    MYT = pytz.timezone("Asia/Kuala_Lumpur")
    now_myt = datetime.datetime.now(tz=MYT)
    myt_date_str = now_myt.strftime("%A, %B ") + str(now_myt.day) + now_myt.strftime(", %Y")

    supabase = await get_supabase()
    supervisor = make_supervisor(supabase, myt_date_str)

    async def on_complete(accumulated_messages):
        """Filter accumulated messages to routing-relevant only and emit lg_history."""
        full_history = list(messages) + list(accumulated_messages)
        relevant = [m for m in full_history if is_routing_relevant(m)]
        stored = messages_to_dict(relevant)
        yield {"data": json.dumps({"type": "lg_history", "messages": stored})}

    async def event_gen():
        completed_normally = False
        try:
            lg_stream = supervisor.astream(
                {"messages": messages},
                config={"recursion_limit": 50},
                stream_mode=["messages", "updates", "custom"],
                subgraphs=True,
            )
            async for event in pipe_langgraph_stream(lg_stream, request_id, on_complete):
                yield event
                # Detect normal completion
                if event.get("data") and '"type": "done"' in event["data"]:
                    completed_normally = True
        except Exception as e:
            yield {"data": json.dumps({"type": "error", "message": str(e)})}
        finally:
            was_stopped = stop_signals.pop(request_id, False) if request_id else False
            if was_stopped and not completed_normally:
                yield {"data": json.dumps({"type": "stopped"})}

    return EventSourceResponse(event_gen())
