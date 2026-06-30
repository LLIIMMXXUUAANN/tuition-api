## Timetable slot generation (`app/features/timetable/`)

### API routes (`router.py`)

- `GET /timetable/rules` / `POST /timetable/rules` — read/write `timetable_rules` in the `settings` table (tutor-only)
- `GET /timetable/buffer-mins` / `POST /timetable/buffer-mins` — read/write `timetable_buffer_mins`; POST validates 0–60; GET guards `int()` conversion with `try/except (ValueError, TypeError)` and raises HTTP 500 with a clear message if the stored value is non-numeric (tutor-only)
- `POST /timetable/generate-slots` — receives `{ rules, studentAvailability?, bookedSlots, bufferMins }`; runs the full slot classification pipeline and returns `{ slots: ClassifiedSlot[] }` (tutor-only)

### Slot classification algorithm (`app/shared/gemini/slot_generation.py` + `timetable/service.py`)

The `POST /timetable/generate-slots` pipeline:

1. **Buffer zone computation (in code — not delegated to the LLM).** A slot is buffered if the gap between it and any booked class is `< bufferMins`. `compute_buffer_slots()` applies deterministic time arithmetic; `build_booked_cell_set()` precomputes which 30-min `TIME_SLOTS` overlap with a booked class. Booked and buffer slots are never sent to Gemini for classification.

2. **Classifiable slots** (non-booked, non-buffered) are enumerated and sent to Gemini 2.5 Flash as a prompt. The response schema is `{ slots: [{ day, time, state: "preferred" | "normal" | "unavailable" }] }` using `responseMimeType: "application/json"` + a Pydantic `responseSchema`.

3. **Post-processing safety net.** After Gemini responds, any buffer slot that the model incorrectly classified as non-unavailable is forced to `"unavailable"`. Zod/Pydantic validates the full response before the safety net runs.

### Prompt rules (important — not obvious from code)

These rules are embedded in `build_slot_prompt()` and constrain how the LLM interprets the `studentAvailability` text:

- **Silence does not imply unavailability.** Student availability text describes only times they _can_ attend. If a time is unmentioned, classify it as `"normal"`, not `"unavailable"`. The LLM must not infer unavailability from missing information.

- **Unavailable-time end boundaries are exclusive.** `"08:00 to 10:00 unavailable"` blocks the 08:00, 08:30, 09:00, 09:30 slots — but NOT 10:00. The LLM is explicitly told to never apply any margin around unavailability boundaries. (Buffer zones around booked student classes are computed in code before the prompt is built and are never left to the model.)

### `ClassifiedSlot` type

```python
class ClassifiedSlot(BaseModel):
    day: str    # e.g. "Monday"
    time: str   # e.g. "09:00"
    state: str  # "preferred" | "normal" | "unavailable" | "booked" | "buffer"
```

Booked and buffer slots are added by the backend (not Gemini) before the response is returned.
