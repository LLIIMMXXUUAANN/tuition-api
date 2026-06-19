"""Subagent builder — port of src/features/agent/lib/lg/progressive.ts."""

from __future__ import annotations

from typing import Callable

from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.features.agent.lg.agent_state import AgentState

TERMINAL_TOOLS = {"final_answer", "cannot_complete"}


def build_subagent(
    name: str,
    description: str | None = None,
    llm=None,
    tools: list | None = None,
    prompt: str | None = None,
    post_tool_hook: Callable | None = None,
):
    """Build a standard ReAct subagent graph.

    Graph structure:
        START → agent ──(tool_calls?)──► tools → [post_hook] → (terminal?) → END
                      └──(no calls)──────────────────────────────────────── END
                                                               └──(no)──► agent
    """
    tools = tools or []
    model = llm.bind_tools(tools)

    async def agent_node(state: AgentState, config: RunnableConfig = None):
        if prompt:
            msgs = [SystemMessage(content=prompt)] + state["messages"]
        else:
            msgs = state["messages"]
        response = await model.ainvoke(msgs, config=config)
        return {"messages": [response]}

    def should_continue(state: AgentState):
        last = state["messages"][-1]
        if isinstance(last, (AIMessage, AIMessageChunk)) and getattr(last, "tool_calls", None):
            return "tools"
        return END

    def route_after_tools(state: AgentState):
        for m in reversed(state["messages"]):
            if isinstance(m, (AIMessage, AIMessageChunk)):
                tool_calls = getattr(m, "tool_calls", None) or []
                if any(tc.get("name") in TERMINAL_TOOLS for tc in tool_calls):
                    return END
                return "agent"
        return "agent"

    tool_node = ToolNode(tools)
    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tool_node)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", should_continue, ["tools", END])

    if post_tool_hook:
        builder.add_node("post_hook", post_tool_hook)
        builder.add_edge("tools", "post_hook")
        builder.add_conditional_edges("post_hook", route_after_tools, ["agent", END])
    else:
        builder.add_conditional_edges("tools", route_after_tools, ["agent", END])

    compiled = builder.compile()
    compiled.name = name
    if description:
        compiled.description = description
    return compiled
