"""Subagent builder — port of src/features/agent/lib/lg/progressive.ts."""

from __future__ import annotations

from typing import Callable

from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode


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
        START → agent ──(tool_calls?)──► tools → [post_hook] → agent
                      └──(no calls)───────────────────────────► END
    """
    tools = tools or []
    model = llm.bind_tools(tools)

    async def agent_node(state: MessagesState, config: RunnableConfig = None):
        if prompt:
            msgs = [SystemMessage(content=prompt)] + state["messages"]
        else:
            msgs = state["messages"]
        response = await model.ainvoke(msgs, config=config)
        return {"messages": [response]}

    def should_continue(state: MessagesState):
        last = state["messages"][-1]
        if isinstance(last, (AIMessage, AIMessageChunk)) and getattr(last, "tool_calls", None):
            return "tools"
        return END

    tool_node = ToolNode(tools)
    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tool_node)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", should_continue, ["tools", END])

    if post_tool_hook:
        builder.add_node("post_hook", post_tool_hook)
        builder.add_edge("tools", "post_hook")
        builder.add_edge("post_hook", "agent")
    else:
        builder.add_edge("tools", "agent")

    compiled = builder.compile()
    compiled.name = name
    if description:
        compiled.description = description
    return compiled
