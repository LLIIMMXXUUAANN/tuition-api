"""Handoff/dispatch tool for the supervisor — port of src/features/agent/lib/lg/handoff.ts."""

from __future__ import annotations

import re

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


def normalize_agent_name(name: str) -> str:
    """Slugify agent name: lowercase, spaces to underscores."""
    return re.sub(r"\s+", "_", name.strip()).lower()


class HandoffTask(BaseModel):
    agentName: str
    task: str


def create_dispatch_tool(agents: list[dict]) -> StructuredTool:
    """Create the 'dispatch' routing tool for the supervisor.

    The tool function itself never runs — supervisorNode intercepts the call
    and emits Send commands directly. Only the schema is needed so the LLM
    knows the tool interface.
    """
    agent_names = [a["name"] for a in agents]
    agent_info = "\n".join(
        f'- **{a["name"]}**: {a.get("description", "Specialist agent")}' for a in agents
    )

    class HandoffEntry(BaseModel):
        agentName: str = Field(
            description=f"Which agent handles this task. One of: {', '.join(agent_names)}"
        )
        task: str = Field(
            description=(
                "Clear, self-contained task for the subagent. Resolve all ambiguities: "
                '"today" → actual date, "this month" → month/year. '
                'Example: "Get the class schedule for Tuesday 2026-05-19."'
            )
        )

    class DispatchInput(BaseModel):
        handoffs: list[HandoffEntry] = Field(
            min_length=1,
            description="List of tasks to dispatch. Use multiple entries for parallel execution.",
        )

    async def _dispatch_impl(handoffs: list[HandoffEntry]) -> str:
        return f"Dispatching {len(handoffs)} task(s)"

    return StructuredTool.from_function(
        coroutine=_dispatch_impl,
        name="dispatch",
        description=(
            f"Route one or more tasks to specialist subagents in parallel. "
            f"Call this ONCE with all tasks you want to run — they execute in parallel.\n\n"
            f"Available agents:\n{agent_info}"
        ),
        args_schema=DispatchInput,
    )
