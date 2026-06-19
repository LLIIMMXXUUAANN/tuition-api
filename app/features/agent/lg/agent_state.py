"""Shared LangGraph state type for the multi-agent system."""

from __future__ import annotations

import operator
from typing import Annotated

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    audit_log: Annotated[list[str], operator.add]
