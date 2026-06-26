"""Agent SSE streaming endpoints — port of src/app/api/agent/chat/route.ts and stop/route.ts.

Also includes /lg/chat endpoint (LangGraph multi-agent) — port of src/app/api/agent/lg/chat/route.ts.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import time
import traceback

import pytz
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from google.genai import types
from langchain_core.messages import (
    AIMessage as LCAIMessage,
    HumanMessage as LCHumanMessage,
    messages_from_dict,
    messages_to_dict,
)
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.features.agent import persistence
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
    get_timetable_settings,
    list_students,
    list_templates,
    manage_portal_access,
    run_sync_all,
    search_students,
    update_buffer_mins,
    update_student,
    update_timetable_rules,
    get_template,
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
# Conversation management endpoints
# ---------------------------------------------------------------------------


@router.get("/conversations/current")
async def get_current_conversation():
    supabase = await get_supabase()
    return await persistence.get_or_create_conversation(supabase)


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str):
    supabase = await get_supabase()
    msgs = await persistence.get_messages(supabase, conversation_id)
    return {"messages": msgs}


@router.post("/conversations/{conversation_id}/clear")
async def clear_conversation(conversation_id: str):
    supabase = await get_supabase()
    await persistence.clear_conversation(supabase, conversation_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Chat endpoint (classic Gemini single-agent)
# ---------------------------------------------------------------------------


class AgentChatRequest(BaseModel):
    conversation_id: str
    message: str | None = None
    request_id: str | None = None
    retry_message_id: str | None = None
    edit_user_message_id: str | None = None
    new_content: str | None = None


@router.post("/chat")
async def agent_chat(body: AgentChatRequest):
    supabase = await get_supabase()
    request_id = body.request_id

    # Build MYT date string (cross-platform: no %-d)
    MYT = pytz.timezone("Asia/Kuala_Lumpur")
    now_myt = datetime.datetime.now(tz=MYT)
    myt_date_str = now_myt.strftime("%A, %B ") + str(now_myt.day) + now_myt.strftime(", %Y")
    system_instruction = f"Today is {myt_date_str} (Malaysia Time).\n\n{SYSTEM_INSTRUCTION}"

    # --- Determine send path and load history ---
    conv = await persistence.get_conversation(supabase, body.conversation_id)
    if conv is None:
        return JSONResponse({"error": "Conversation not found"}, status_code=404)

    user_message_content: str
    gemini_history_raw: list = conv.get("gemini_contents") or []
    agent_msg_id_to_update: str | None = None

    if body.retry_message_id:
        # Retry: failed turn never updated gemini_contents, so it's already the pre-failure state
        failed = await persistence.get_message_by_id(supabase, body.retry_message_id)
        if not failed:
            return JSONResponse({"error": "Message not found"}, status_code=404)
        prior_user = await persistence.get_preceding_user_message(
            supabase, body.conversation_id, failed["created_at"]
        )
        if not prior_user:
            return JSONResponse({"error": "No preceding user message found"}, status_code=400)
        user_message_content = prior_user["content"]
        agent_msg_id_to_update = failed["id"]
    elif body.edit_user_message_id:
        # Edit: use prev_gemini_contents (state before the latest completed turn)
        old_user = await persistence.get_message_by_id(supabase, body.edit_user_message_id)
        if not old_user:
            return JSONResponse({"error": "Message not found"}, status_code=404)
        gemini_history_raw = conv.get("prev_gemini_contents") or []
        user_message_content = body.new_content or ""
        await persistence.delete_messages_from(supabase, body.conversation_id, old_user["created_at"])
        await persistence.insert_user_message(supabase, body.conversation_id, user_message_content)
        # Preemptive write: if the LLM fails, retry reads gemini_contents directly —
        # reset it now so retry uses the correct pre-edit state instead of the stale post-turn state.
        await persistence.update_conversation_history(
            supabase, body.conversation_id, gemini_contents=gemini_history_raw
        )
    else:
        # Normal send
        user_message_content = body.message or ""
        await persistence.insert_user_message(supabase, body.conversation_id, user_message_content)

    # Pre-insert placeholder agent row BEFORE streaming starts (same pattern as lg_chat).
    pre_agent_id: str | None = None
    if not agent_msg_id_to_update:
        try:
            pre_agent_id = await persistence.pre_insert_agent_message(
                supabase, body.conversation_id
            )
        except Exception:
            pass  # non-fatal — falls back to INSERT in save paths

    async def event_gen():
        # Build initial contents from DB history
        contents: list[types.Content]
        if gemini_history_raw:
            contents = [_to_content(c) for c in gemini_history_raw]
            contents.append(types.Content(role="user", parts=[types.Part(text=user_message_content)]))
        else:
            contents = [types.Content(role="user", parts=[types.Part(text=user_message_content)])]

        accumulated_content: list[str] = []
        accumulated_steps: list[str] = []
        accumulated_students: list | None = None
        accumulated_schedule_students: list | None = None
        accumulated_slot_data: list | None = None
        got_reply = False
        agent_msg_saved = False
        completed_normally = False

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
                                    accumulated_content.append(flush)
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
                        accumulated_students = students
                        yield {"data": json.dumps({"type": "ui_action", "action": "student_links", "payload": {"studentLinks": students}})}
                    if cleaned:
                        accumulated_content.append(cleaned)
                        yield {"data": json.dumps({"type": "chunk", "content": cleaned})}
                    break

                named_calls = [fc for fc in round_fn_calls if fc.name]

                # Emit step events before parallel execution (stable display order)
                for fc in named_calls:
                    step_text = f"🔧 {fc.name}({json.dumps(dict(fc.args or {}))})"
                    accumulated_steps.append(step_text)
                    yield {"data": json.dumps({"type": "step", "content": step_text})}

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
                    step_text = f"⏱ parallel ×{len(named_calls)} — {timing_str} (total {round_ms}ms)"
                    accumulated_steps.append(step_text)
                    yield {"data": json.dumps({"type": "step", "content": step_text})}

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
                    if event.get("action") == "slots_ready":
                        accumulated_slot_data = event.get("payload", {}).get("slots")
                    elif event.get("action") == "download_schedule":
                        accumulated_schedule_students = event.get("payload", {}).get("students")
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
                            accumulated_steps.append(verdict)
                            yield {"data": json.dumps({"type": "step", "content": verdict})}

            # Post-loop: determine outcome (inside try so finally still covers abort)
            was_stopped = bool(stop_signals.pop(request_id, False)) if request_id else False

            if not got_reply and not was_stopped:
                fallback = "I wasn't able to complete that in the allowed steps — please try a simpler request."
                accumulated_content.append(fallback)
                yield {"data": json.dumps({"type": "chunk", "content": fallback})}

            if was_stopped:
                effective_id = agent_msg_id_to_update or pre_agent_id
                try:
                    if effective_id:
                        await persistence.update_agent_message(
                            supabase, effective_id,
                            content="".join(accumulated_content),
                            steps=accumulated_steps,
                            is_error=True,
                        )
                    else:
                        await persistence.insert_agent_message(
                            supabase, body.conversation_id,
                            content="".join(accumulated_content),
                            steps=accumulated_steps,
                            is_error=True,
                        )
                    agent_msg_saved = True
                except Exception:
                    pass
                yield {"data": json.dumps({"type": "stopped"})}
            else:
                effective_id = agent_msg_id_to_update or pre_agent_id
                history_dicts = [_content_to_dict(c) for c in contents]
                try:
                    if effective_id:
                        await persistence.update_agent_message(
                            supabase, effective_id,
                            content="".join(accumulated_content),
                            steps=accumulated_steps,
                            students=accumulated_students,
                            schedule_students=accumulated_schedule_students,
                            slot_data=accumulated_slot_data,
                        )
                    else:
                        await persistence.insert_agent_message(
                            supabase, body.conversation_id,
                            content="".join(accumulated_content),
                            steps=accumulated_steps,
                            students=accumulated_students,
                            schedule_students=accumulated_schedule_students,
                            slot_data=accumulated_slot_data,
                        )
                    await persistence.update_conversation_history(
                        supabase, body.conversation_id,
                        gemini_contents=history_dicts,
                        prev_gemini_contents=gemini_history_raw,
                    )
                    agent_msg_saved = True
                except Exception:
                    pass
                yield {"data": json.dumps({"type": "done"})}
                completed_normally = True

        except Exception as e:
            if request_id:
                stop_signals.pop(request_id, None)
            effective_id = agent_msg_id_to_update or pre_agent_id
            try:
                if effective_id:
                    await persistence.update_agent_message(
                        supabase, effective_id,
                        content="".join(accumulated_content),
                        steps=accumulated_steps,
                        is_error=True,
                    )
                else:
                    await persistence.insert_agent_message(
                        supabase, body.conversation_id,
                        content="".join(accumulated_content),
                        steps=accumulated_steps,
                        is_error=True,
                    )
                agent_msg_saved = True
            except Exception:
                pass
            yield {"data": json.dumps({"type": "error", "message": str(e)})}

        finally:
            if not completed_normally and not agent_msg_saved:
                effective_id = agent_msg_id_to_update or pre_agent_id
                try:
                    if effective_id:
                        await persistence.update_agent_message(
                            supabase, effective_id,
                            content="".join(accumulated_content),
                            steps=accumulated_steps,
                            is_error=True,
                        )
                    else:
                        await persistence.insert_agent_message(
                            supabase, body.conversation_id,
                            content="".join(accumulated_content),
                            steps=accumulated_steps,
                            is_error=True,
                        )
                except Exception:
                    pass

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
async def lg_chat(body: AgentChatRequest):
    supabase = await get_supabase()
    request_id = body.request_id

    # MYT date string (cross-platform: no %-d)
    MYT = pytz.timezone("Asia/Kuala_Lumpur")
    now_myt = datetime.datetime.now(tz=MYT)
    myt_date_str = now_myt.strftime("%A, %B ") + str(now_myt.day) + now_myt.strftime(", %Y")

    # --- Determine send path and load history ---
    conv = await persistence.get_conversation(supabase, body.conversation_id)
    if conv is None:
        return JSONResponse({"error": "Conversation not found"}, status_code=404)

    user_message_content: str
    lg_history_raw: list = conv.get("lg_contents") or []
    agent_msg_id_to_update: str | None = None

    if body.retry_message_id:
        # Retry: failed turn never updated lg_contents, so it's already the pre-failure state
        failed = await persistence.get_message_by_id(supabase, body.retry_message_id)
        if not failed:
            return JSONResponse({"error": "Message not found"}, status_code=404)
        prior_user = await persistence.get_preceding_user_message(
            supabase, body.conversation_id, failed["created_at"]
        )
        if not prior_user:
            return JSONResponse({"error": "No preceding user message found"}, status_code=400)
        user_message_content = prior_user["content"]
        agent_msg_id_to_update = failed["id"]
    elif body.edit_user_message_id:
        # Edit: use prev_lg_contents (state before the latest completed turn)
        old_user = await persistence.get_message_by_id(supabase, body.edit_user_message_id)
        if not old_user:
            return JSONResponse({"error": "Message not found"}, status_code=404)
        lg_history_raw = conv.get("prev_lg_contents") or []
        user_message_content = body.new_content or ""
        await persistence.delete_messages_from(supabase, body.conversation_id, old_user["created_at"])
        await persistence.insert_user_message(supabase, body.conversation_id, user_message_content)
        # Preemptive write: if the LLM fails, retry reads lg_contents directly —
        # reset it now so retry uses the correct pre-edit state instead of the stale post-turn state.
        await persistence.update_conversation_history(
            supabase, body.conversation_id, lg_contents=lg_history_raw
        )
    else:
        user_message_content = body.message or ""
        await persistence.insert_user_message(supabase, body.conversation_id, user_message_content)

    # Build LangGraph message list
    if lg_history_raw:
        restored = messages_from_dict(lg_history_raw)
        messages = restored + [LCHumanMessage(user_message_content)]
    else:
        messages = [LCHumanMessage(user_message_content)]

    supervisor = make_supervisor(supabase, myt_date_str)

    # Pre-insert placeholder agent row BEFORE streaming starts so the row is in DB
    # before ANY SSE byte is sent. Fast page reloads always find it (is_error=True
    # default; on_complete flips to False on success).
    pre_agent_id: str | None = None
    if not agent_msg_id_to_update:
        try:
            pre_agent_id = await persistence.pre_insert_agent_message(
                supabase, body.conversation_id
            )
        except Exception:
            pass  # non-fatal — falls back to INSERT in on_complete / finally

    async def event_gen():
        accumulated_content: list[str] = []
        accumulated_steps: list[str] = []
        accumulated_students: list | None = None
        accumulated_schedule_students: list | None = None
        accumulated_slot_data: list | None = None
        completed_normally = False
        agent_msg_saved = False

        async def on_complete(accumulated_messages):
            nonlocal agent_msg_saved
            """Save to DB instead of emitting lg_history SSE event."""
            try:
                full_history = list(messages) + list(accumulated_messages)
                relevant = [m for m in full_history if is_routing_relevant(m)]
                stored = messages_to_dict(relevant)
            except Exception:
                print("[on_complete] messages_to_dict failed:")
                traceback.print_exc()
                stored = None

            effective_id = agent_msg_id_to_update or pre_agent_id
            try:
                if effective_id:
                    await persistence.update_agent_message(
                        supabase, effective_id,
                        content="".join(accumulated_content),
                        steps=accumulated_steps,
                        students=accumulated_students,
                        schedule_students=accumulated_schedule_students,
                        slot_data=accumulated_slot_data,
                    )
                else:
                    await persistence.insert_agent_message(
                        supabase, body.conversation_id,
                        content="".join(accumulated_content),
                        steps=accumulated_steps,
                        students=accumulated_students,
                        schedule_students=accumulated_schedule_students,
                        slot_data=accumulated_slot_data,
                    )
                agent_msg_saved = True
                if stored is not None:
                    await persistence.update_conversation_history(
                        supabase, body.conversation_id,
                        lg_contents=stored,
                        prev_lg_contents=lg_history_raw,
                    )
            except Exception:
                print("[on_complete] DB save failed:")
                traceback.print_exc()

        try:
            lg_stream = supervisor.astream(
                {"messages": messages},
                config={"recursion_limit": 50},
                stream_mode=["messages", "updates", "custom"],
                subgraphs=True,
            )
            async for event in pipe_langgraph_stream(lg_stream, request_id, on_complete):
                # Accumulate for DB persistence on stopped/error paths
                data_str = event.get("data", "")
                if data_str:
                    try:
                        parsed = json.loads(data_str)
                        match parsed.get("type"):
                            case "chunk":
                                accumulated_content.append(parsed.get("content", ""))
                            case "step":
                                accumulated_steps.append(parsed.get("content", ""))
                            case "ui_action":
                                action = parsed.get("action")
                                payload = parsed.get("payload", {})
                                if action == "student_links":
                                    accumulated_students = payload.get("studentLinks")
                                elif action == "download_schedule":
                                    accumulated_schedule_students = payload.get("students")
                                elif action == "slots_ready":
                                    accumulated_slot_data = payload.get("slots")
                            case "done":
                                completed_normally = True
                    except Exception:
                        pass
                yield event
        except Exception as e:
            effective_id = agent_msg_id_to_update or pre_agent_id
            try:
                if effective_id:
                    await persistence.update_agent_message(
                        supabase, effective_id,
                        content="".join(accumulated_content),
                        steps=accumulated_steps,
                        is_error=True,
                    )
                else:
                    await persistence.insert_agent_message(
                        supabase, body.conversation_id,
                        content="".join(accumulated_content),
                        steps=accumulated_steps,
                        is_error=True,
                    )
                agent_msg_saved = True
            except Exception:
                pass
            yield {"data": json.dumps({"type": "error", "message": str(e)})}
        finally:
            was_stopped = stop_signals.pop(request_id, False) if request_id else False
            if not completed_normally and not agent_msg_saved:
                effective_id = agent_msg_id_to_update or pre_agent_id
                try:
                    if effective_id:
                        await persistence.update_agent_message(
                            supabase, effective_id,
                            content="".join(accumulated_content),
                            steps=accumulated_steps,
                            is_error=True,
                        )
                    else:
                        await persistence.insert_agent_message(
                            supabase, body.conversation_id,
                            content="".join(accumulated_content),
                            steps=accumulated_steps,
                            is_error=True,
                        )
                except Exception:
                    pass
                if was_stopped:
                    yield {"data": json.dumps({"type": "stopped"})}

    return EventSourceResponse(event_gen())
