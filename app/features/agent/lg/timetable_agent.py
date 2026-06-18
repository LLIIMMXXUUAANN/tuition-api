"""Timetable subagent — port of src/features/agent/lib/lg/timetable-agent.ts."""

from __future__ import annotations

from app.features.agent.lg.model import get_gemini_chat_model
from app.features.agent.lg.post_hooks import make_timetable_post_hook
from app.features.agent.lg.subagent import build_subagent
from app.features.agent.lg.tool_factories import make_timetable_tools

TIMETABLE_PROMPT = """\
You are the timetable agent in a multi-agent tuition admin system. You handle the scheduling-rules text, the buffer-minutes setting, AI slot-availability generation, and downloading the weekly schedule PNG.

RULES:
1. Use get_timetable_settings to read current rules and buffer before updating. When the user asks to update rules, show them the proposed new rules and confirm before calling update_timetable_rules. For update_buffer_mins, validate the value is 0–60 before calling.
2. After calling generate_slot_availability or download_timetable_image, tell the user a download button has appeared in the chat. Do NOT describe slot counts or classification details unless the user asks — keep the reply brief (one sentence).
3. generate_slot_availability accepts an optional student_availability string. Pass it only if the user described a prospective student's availability; otherwise omit it.
4. If the task cannot be completed with your available tools, call cannot_complete(reason="...") explaining why.\
"""


def make_timetable_agent(supabase):
    """Build and return the timetable subagent."""
    tools = make_timetable_tools(supabase)
    post_hook = make_timetable_post_hook(supabase)
    return build_subagent(
        name="timetable_agent",
        description="Handles timetable scheduling rules, buffer minutes, slot-availability generation, and weekly schedule PNG download",
        llm=get_gemini_chat_model(),
        tools=tools,
        prompt=TIMETABLE_PROMPT,
        post_tool_hook=post_hook,
    )
