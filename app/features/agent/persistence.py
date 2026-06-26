"""Supabase persistence for agent conversation history."""

from __future__ import annotations

from datetime import datetime, timezone

from app.features.agent.tools.shared import SupabaseClient


async def create_conversation(supabase: SupabaseClient) -> str:
    res = await supabase.table("agent_conversations").insert({}).execute()
    return res.data[0]["id"]


async def get_or_create_conversation(supabase: SupabaseClient) -> dict:
    """Return the most recent conversation (creating one if none exists) with its messages."""
    res = (
        await supabase.table("agent_conversations")
        .select("id")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if res.data:
        conv_id = res.data[0]["id"]
    else:
        conv_id = await create_conversation(supabase)
    msgs = await get_messages(supabase, conv_id)
    return {"id": conv_id, "messages": msgs}


async def clear_conversation(supabase: SupabaseClient, conversation_id: str) -> None:
    """Delete all messages and reset LLM history for a conversation, keeping the row."""
    await supabase.table("agent_messages").delete().eq("conversation_id", conversation_id).execute()
    await supabase.table("agent_conversations").update({
        "lg_contents": None,
        "prev_lg_contents": None,
    }).eq("id", conversation_id).execute()


async def get_conversation(supabase: SupabaseClient, conversation_id: str) -> dict | None:
    res = (
        await supabase.table("agent_conversations")
        .select("*")
        .eq("id", conversation_id)
        .maybe_single()
        .execute()
    )
    return res.data


async def update_conversation_history(
    supabase: SupabaseClient,
    conversation_id: str,
    *,
    lg_contents: list | None = None,
    prev_lg_contents: list | None = None,
) -> None:
    updates: dict = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if lg_contents is not None:
        updates["lg_contents"] = lg_contents
    if prev_lg_contents is not None:
        updates["prev_lg_contents"] = prev_lg_contents
    await supabase.table("agent_conversations").update(updates).eq("id", conversation_id).execute()


async def insert_user_message(supabase: SupabaseClient, conversation_id: str, content: str) -> str:
    res = await supabase.table("agent_messages").insert({
        "conversation_id": conversation_id,
        "role": "user",
        "content": content,
    }).execute()
    return res.data[0]["id"]


async def pre_insert_agent_message(supabase: SupabaseClient, conversation_id: str) -> str:
    """Insert a placeholder agent row (is_error=True) that will be updated on completion.

    Ensures the row is in DB before streaming starts so fast page reloads always find it.
    Defaults to is_error=True so any abandoned row is retriable.
    """
    res = await supabase.table("agent_messages").insert({
        "conversation_id": conversation_id,
        "role": "agent",
        "content": "",
        "steps": [],
        "is_error": True,
    }).execute()
    return res.data[0]["id"]


async def insert_agent_message(
    supabase: SupabaseClient,
    conversation_id: str,
    *,
    content: str,
    steps: list[str],
    is_error: bool = False,
    students: list | None = None,
    schedule_students: list | None = None,
    slot_data: list | None = None,
) -> str:
    row: dict = {
        "conversation_id": conversation_id,
        "role": "agent",
        "content": content,
        "steps": steps,
        "is_error": is_error,
    }
    if students is not None:
        row["students"] = students
    if schedule_students is not None:
        row["schedule_students"] = schedule_students
    if slot_data is not None:
        row["slot_data"] = slot_data
    res = await supabase.table("agent_messages").insert(row).execute()
    return res.data[0]["id"]


async def update_agent_message(
    supabase: SupabaseClient,
    message_id: str,
    *,
    content: str,
    steps: list[str],
    is_error: bool = False,
    students: list | None = None,
    schedule_students: list | None = None,
    slot_data: list | None = None,
) -> None:
    updates: dict = {
        "content": content,
        "steps": steps,
        "is_error": is_error,
        "students": students,
        "schedule_students": schedule_students,
        "slot_data": slot_data,
    }
    await supabase.table("agent_messages").update(updates).eq("id", message_id).execute()


def _row_to_message(row: dict) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "steps": row.get("steps") or [],
        "isError": row.get("is_error") or False,
        "students": row.get("students"),
        "scheduleStudents": row.get("schedule_students"),
        "slotData": row.get("slot_data"),
        "timestamp": row["created_at"],
    }


async def get_messages(supabase: SupabaseClient, conversation_id: str) -> list[dict]:
    res = (
        await supabase.table("agent_messages")
        .select("*")
        .eq("conversation_id", conversation_id)
        .order("created_at")
        .execute()
    )
    return [_row_to_message(r) for r in (res.data or [])]


async def get_message_by_id(supabase: SupabaseClient, message_id: str) -> dict | None:
    res = (
        await supabase.table("agent_messages")
        .select("*")
        .eq("id", message_id)
        .maybe_single()
        .execute()
    )
    return res.data if res else None


async def get_preceding_user_message(
    supabase: SupabaseClient, conversation_id: str, before_created_at: str
) -> dict | None:
    res = (
        await supabase.table("agent_messages")
        .select("*")
        .eq("conversation_id", conversation_id)
        .eq("role", "user")
        .lt("created_at", before_created_at)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


async def delete_message(supabase: SupabaseClient, message_id: str) -> None:
    await supabase.table("agent_messages").delete().eq("id", message_id).execute()


async def delete_messages_from(
    supabase: SupabaseClient, conversation_id: str, from_created_at: str
) -> None:
    await (
        supabase.table("agent_messages")
        .delete()
        .eq("conversation_id", conversation_id)
        .gte("created_at", from_created_at)
        .execute()
    )
