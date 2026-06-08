"""LangGraph model factory — port of src/features/agent/lib/lg/model.ts."""

from __future__ import annotations

from langchain_google_genai import ChatGoogleGenerativeAI

from app.config import settings


def get_gemini_chat_model() -> ChatGoogleGenerativeAI:
    """Fresh instance per call — parallel subagents must not share a model instance.

    thinking_budget=0 disables Gemini 2.5 Flash's thinking pass which exhausts
    token budget on large tool schemas (11 tools) and returns empty response.
    """
    try:
        return ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            temperature=0,
            google_api_key=settings.gemini_api_key,
            thinking_budget=0,  # disable thinking pass
        )
    except TypeError:
        # Older versions of langchain-google-genai may not accept thinking_budget
        return ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            temperature=0,
            google_api_key=settings.gemini_api_key,
        )
