"""Agent SSE streaming endpoint — LangGraph multi-agent."""

from __future__ import annotations

import datetime
import json
import logging

logger = logging.getLogger(__name__)

import pytz
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from langchain_core.messages import (
    HumanMessage as LCHumanMessage,
    messages_from_dict,
    messages_to_dict,
)
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from supabase import AsyncClient

from app.features.agent import persistence
from app.features.agent.lg.stream_adapter import is_routing_relevant, pipe_langgraph_stream
from app.features.agent.lg.supervisor import make_supervisor
from app.features.agent.state import stop_signals
from app.shared.response_models import ConversationResponse, MessagesResponse, OkResponse
from app.auth import require_internal_secret
from app.shared.db import get_supabase

router = APIRouter(dependencies=[Depends(require_internal_secret)], tags=["agent"])


# ---------------------------------------------------------------------------
# Conversation management endpoints
# ---------------------------------------------------------------------------


@router.get("/conversations/current", response_model=ConversationResponse)
async def get_current_conversation(supabase: AsyncClient = Depends(get_supabase)):
    return await persistence.get_or_create_conversation(supabase)


@router.get("/conversations/{conversation_id}/messages", response_model=MessagesResponse)
async def get_conversation_messages(conversation_id: str, supabase: AsyncClient = Depends(get_supabase)):
    msgs = await persistence.get_messages(supabase, conversation_id)
    return {"messages": msgs}


@router.post("/conversations/{conversation_id}/clear", response_model=OkResponse)
async def clear_conversation(conversation_id: str, supabase: AsyncClient = Depends(get_supabase)):
    await persistence.clear_conversation(supabase, conversation_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Chat endpoint (LangGraph multi-agent)
# ---------------------------------------------------------------------------


class AgentChatRequest(BaseModel):
    conversation_id: str
    message: str | None = None
    request_id: str | None = None
    retry_message_id: str | None = None
    edit_user_message_id: str | None = None
    new_content: str | None = None


@router.post("/chat")
async def agent_chat(body: AgentChatRequest, supabase: AsyncClient = Depends(get_supabase)):
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

    async def event_gen(sb: AsyncClient):
        accumulated_content: list[str] = []
        accumulated_steps: list[str] = []
        accumulated_students: list | None = None
        accumulated_schedule_students: list | None = None
        accumulated_slot_data: list | None = None
        completed_normally = False
        agent_msg_saved = False

        async def on_complete(accumulated_messages):
            nonlocal agent_msg_saved
            """Save to DB after stream completes."""
            try:
                full_history = list(messages) + list(accumulated_messages)
                relevant = [m for m in full_history if is_routing_relevant(m)]
                stored = messages_to_dict(relevant)
            except Exception:
                logger.exception("[on_complete] messages_to_dict failed")
                stored = None

            effective_id = agent_msg_id_to_update or pre_agent_id
            try:
                if effective_id:
                    await persistence.update_agent_message(
                        sb, effective_id,
                        content="".join(accumulated_content),
                        steps=accumulated_steps,
                        students=accumulated_students,
                        schedule_students=accumulated_schedule_students,
                        slot_data=accumulated_slot_data,
                    )
                else:
                    await persistence.insert_agent_message(
                        sb, body.conversation_id,
                        content="".join(accumulated_content),
                        steps=accumulated_steps,
                        students=accumulated_students,
                        schedule_students=accumulated_schedule_students,
                        slot_data=accumulated_slot_data,
                    )
                agent_msg_saved = True
                if stored is not None:
                    await persistence.update_conversation_history(
                        sb, body.conversation_id,
                        lg_contents=stored,
                        prev_lg_contents=lg_history_raw,
                    )
            except Exception:
                logger.exception("[on_complete] DB save failed")

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
                                    accumulated_students = payload.get("student_links")
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
                        sb, effective_id,
                        content="".join(accumulated_content),
                        steps=accumulated_steps,
                        is_error=True,
                    )
                else:
                    await persistence.insert_agent_message(
                        sb, body.conversation_id,
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
                            sb, effective_id,
                            content="".join(accumulated_content),
                            steps=accumulated_steps,
                            is_error=True,
                        )
                    else:
                        await persistence.insert_agent_message(
                            sb, body.conversation_id,
                            content="".join(accumulated_content),
                            steps=accumulated_steps,
                            is_error=True,
                        )
                except Exception:
                    pass
                if was_stopped:
                    yield {"data": json.dumps({"type": "stopped"})}

    return EventSourceResponse(event_gen(supabase))


# ---------------------------------------------------------------------------
# Stop endpoint
# ---------------------------------------------------------------------------


class StopRequest(BaseModel):
    request_id: str | None = None


@router.post("/stop", response_model=OkResponse)
async def stop_agent(body: StopRequest):
    if body.request_id:
        stop_signals[body.request_id] = True
    return {"ok": True}
