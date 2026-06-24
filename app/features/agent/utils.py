import re

_STUDENT_TOKEN_RE = re.compile(r'\[student_id:([^:\]]+):([0-9a-f-]+)\]', re.IGNORECASE)

TRAILING_BUFFER = 250


def extract_student_tokens(text: str) -> tuple[str, list[dict]]:
    """Strip [student_id:NAME:UUID] tokens from text. Returns (cleaned_text, unique_students)."""
    students: list[dict] = []
    seen: set[str] = set()
    for m in _STUDENT_TOKEN_RE.finditer(text):
        sid = m.group(2)
        if sid not in seen:
            seen.add(sid)
            students.append({"name": m.group(1).strip(), "id": sid})
    cleaned = _STUDENT_TOKEN_RE.sub('', text).strip()
    return cleaned, students
