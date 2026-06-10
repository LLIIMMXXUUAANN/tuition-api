"""Gemini slot generation — port of src/services/gemini/index.ts."""

from __future__ import annotations

import json

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.types import SlotState


class ClassifiedSlot(BaseModel):
    day: str
    time: str
    state: SlotState


class GenerateSlotsResponse(BaseModel):
    slots: list[ClassifiedSlot]


_RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "slots": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "day": types.Schema(type=types.Type.STRING),
                    "time": types.Schema(type=types.Type.STRING),
                    "state": types.Schema(
                        type=types.Type.STRING,
                        enum=["preferred", "normal", "unavailable"],
                    ),
                },
                required=["day", "time", "state"],
            ),
        )
    },
    required=["slots"],
)


async def run_gemini_slot_generation(prompt: str) -> list[ClassifiedSlot]:
    """Call Gemini 2.5 Flash with structured output; return validated slot list."""
    client = genai.Client(api_key=settings.gemini_api_key)

    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_RESPONSE_SCHEMA,
            temperature=0,
        ),
    )

    raw = json.loads(response.text)

    try:
        parsed = GenerateSlotsResponse.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Gemini response failed validation: {exc}") from exc

    return parsed.slots
