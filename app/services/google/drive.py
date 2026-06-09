import asyncio
import io
import json
import re
from functools import partial

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

from app.config import settings
from app.types import ClassSlot

_DRIVE = "https://www.googleapis.com/drive/v3"
_UPLOAD = "https://www.googleapis.com/upload/drive/v3"

EMPTY_IPYNB = json.dumps({
    "cells": [
        {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": [],
        }
    ],
    "metadata": {
        "kernelspec": {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        },
        "language_info": {"name": "python", "version": "3.12.0"},
    },
    "nbformat": 4,
    "nbformat_minor": 4,
})


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fmt(time_str: str) -> str:
    """Formats 'HH:MM' → '2:30pm' or '2pm' (no minutes if :00)."""
    h, m = (int(x) for x in time_str.split(":"))
    period = "pm" if h >= 12 else "am"
    hour = h % 12 or 12
    if m == 0:
        return f"{hour}{period}"
    return f"{hour}:{str(m).zfill(2)}{period}"


def build_meet_doc_html(student_name: str, schedule: list[ClassSlot], meet_link: str) -> str:
    slot_lines = "<br>".join(
        f"{_esc(s.day)} · {_fmt(s.start)} – {_fmt(s.end)}" for s in schedule
    )
    safe_link = _esc(meet_link)
    parts = [
        f"<p>{_esc(student_name)}</p>",
        "<p><br></p>",
        f"<p>{slot_lines}</p>",
        "<p><br></p>",
        f'<p>Time zone: Asia/Kuala_Lumpur<br>Google Meet joining info<br>Video call link: <a href="{safe_link}" style="color:#1155CC">{safe_link}</a></p>',
        "<p><br></p><p>The same link will be used for the other time as well.</p>",
    ]
    return "".join(parts)


def parse_drive_folder_id(drive_folder_url: str) -> str:
    """Extracts and validates the folder ID from a Drive URL."""
    parts = drive_folder_url.split("/folders/")
    if len(parts) < 2:
        raise ValueError("Could not parse folder ID from Drive URL")
    raw_id = parts[1].split("?")[0].split("/")[0]
    if not raw_id:
        raise ValueError("Could not parse folder ID from Drive URL")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", raw_id):
        raise ValueError("Invalid folder ID in Drive URL")
    return raw_id


def _session(creds: Credentials) -> AuthorizedSession:
    """Requests session that bypasses system proxy (avoids httplib2 SSL issues on Windows)."""
    s = AuthorizedSession(creds)
    s.trust_env = False
    return s


# ---------------------------------------------------------------------------
# Sync helpers (run in executor)
# ---------------------------------------------------------------------------

def _create_folder(session: AuthorizedSession, name: str, parent_id: str) -> str:
    resp = session.post(
        f"{_DRIVE}/files",
        params={"fields": "id"},
        json={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _set_anyone_reader(session: AuthorizedSession, file_id: str) -> None:
    resp = session.post(
        f"{_DRIVE}/files/{file_id}/permissions",
        json={"role": "reader", "type": "anyone"},
    )
    resp.raise_for_status()


def _create_shortcut(session: AuthorizedSession, target_id: str, parent_id: str, name: str) -> None:
    resp = session.post(
        f"{_DRIVE}/files",
        json={
            "name": name,
            "mimeType": "application/vnd.google-apps.shortcut",
            "parents": [parent_id],
            "shortcutDetails": {"targetId": target_id},
        },
    )
    resp.raise_for_status()


def _upload_multipart(
    session: AuthorizedSession,
    name: str,
    mime_type: str,
    content: bytes,
    parent_id: str,
) -> str:
    metadata = json.dumps({"name": name, "parents": [parent_id]}).encode()
    resp = session.post(
        f"{_UPLOAD}/files",
        params={"uploadType": "multipart", "fields": "id"},
        headers={"Content-Type": f"multipart/related; boundary=__END_OF_PART__"},
        data=(
            b"--__END_OF_PART__\r\n"
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            + metadata + b"\r\n"
            b"--__END_OF_PART__\r\n"
            b"Content-Type: " + mime_type.encode() + b"\r\n\r\n"
            + content + b"\r\n"
            b"--__END_OF_PART__--"
        ),
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _create_blank_doc(session: AuthorizedSession, name: str, parent_id: str) -> None:
    resp = session.post(
        f"{_DRIVE}/files",
        json={"name": name, "mimeType": "application/vnd.google-apps.document", "parents": [parent_id]},
    )
    resp.raise_for_status()


def _upload_html_as_doc(session: AuthorizedSession, name: str, html: bytes, parent_id: str) -> str:
    """Uploads HTML content, importing it as a Google Doc."""
    metadata = json.dumps({
        "name": name,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [parent_id],
    }).encode()
    resp = session.post(
        f"{_UPLOAD}/files",
        params={"uploadType": "multipart", "fields": "id"},
        headers={"Content-Type": "multipart/related; boundary=__END_OF_PART__"},
        data=(
            b"--__END_OF_PART__\r\n"
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            + metadata + b"\r\n"
            b"--__END_OF_PART__\r\n"
            b"Content-Type: text/html\r\n\r\n"
            + html + b"\r\n"
            b"--__END_OF_PART__--"
        ),
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _find_meet_doc_id(session: AuthorizedSession, folder_id: str) -> str | None:
    resp = session.get(
        f"{_DRIVE}/files",
        params={
            "q": (
                f"'{folder_id}' in parents "
                "and name = 'Google Meet Link' "
                "and mimeType = 'application/vnd.google-apps.document' "
                "and trashed = false"
            ),
            "fields": "files(id)",
            "pageSize": 1,
        },
    )
    resp.raise_for_status()
    files = resp.json().get("files", [])
    return files[0]["id"] if files else None


def _update_doc_content(session: AuthorizedSession, doc_id: str, html: bytes) -> None:
    metadata = json.dumps({}).encode()
    resp = session.patch(
        f"{_UPLOAD}/files/{doc_id}",
        params={"uploadType": "multipart"},
        headers={"Content-Type": "multipart/related; boundary=__END_OF_PART__"},
        data=(
            b"--__END_OF_PART__\r\n"
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            + metadata + b"\r\n"
            b"--__END_OF_PART__\r\n"
            b"Content-Type: text/html\r\n\r\n"
            + html + b"\r\n"
            b"--__END_OF_PART__--"
        ),
    )
    resp.raise_for_status()


def _delete_file(session: AuthorizedSession, file_id: str) -> None:
    resp = session.delete(f"{_DRIVE}/files/{file_id}")
    resp.raise_for_status()


def _trash_file(session: AuthorizedSession, file_id: str) -> None:
    resp = session.patch(f"{_DRIVE}/files/{file_id}", json={"trashed": True})
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

async def update_student_meet_doc(
    creds: Credentials,
    drive_folder_url: str,
    student_name: str,
    schedule: list[ClassSlot],
    meet_link: str,
) -> None:
    """Finds the 'Google Meet Link' doc in the student's Drive folder and rewrites its HTML."""
    folder_id = parse_drive_folder_id(drive_folder_url)
    loop = asyncio.get_running_loop()
    session = _session(creds)
    html = build_meet_doc_html(student_name, schedule, meet_link).encode("utf-8")

    doc_id = await loop.run_in_executor(None, partial(_find_meet_doc_id, session, folder_id))
    if not doc_id:
        raise RuntimeError("Google Meet Link doc not found in student Drive folder")

    await loop.run_in_executor(None, partial(_update_doc_content, session, doc_id, html))


async def create_student_drive_folder(
    creds: Credentials,
    student_name: str,
    meet_link: str,
    class_schedule: list[ClassSlot],
    mode: str = "My Python Syllabus",
) -> str:
    """
    Creates the student's Drive folder structure.
    'My Python Syllabus': root + 4 subfolders + Meet doc.
    'Other Syllabus': root + Meet doc only.
    Returns the root folder URL.
    """
    students_folder_id = settings.google_students_folder_id
    lec_topic1_file_id = settings.google_lec_topic1_file_id

    if mode == "My Python Syllabus" and not lec_topic1_file_id:
        raise RuntimeError("GOOGLE_LEC_TOPIC1_FILE_ID env var is not set")

    loop = asyncio.get_running_loop()
    session = _session(creds)
    html = build_meet_doc_html(student_name, class_schedule, meet_link).encode("utf-8")

    root_id = await loop.run_in_executor(
        None, partial(_create_folder, session, student_name, students_folder_id)
    )
    await loop.run_in_executor(None, partial(_set_anyone_reader, session, root_id))

    try:
        if mode == "My Python Syllabus":
            async def _teaching_slides() -> None:
                teaching_id = await loop.run_in_executor(
                    None, partial(_create_folder, session, "1. Teaching Slides", root_id)
                )
                await loop.run_in_executor(
                    None, partial(_create_shortcut, session, lec_topic1_file_id, teaching_id, "Topic_1.pptx")
                )

            async def _coding_examples() -> None:
                coding_id = await loop.run_in_executor(
                    None, partial(_create_folder, session, "2. In-Class Coding Examples", root_id)
                )
                await loop.run_in_executor(
                    None,
                    partial(
                        _upload_multipart, session,
                        f"{student_name} Topic 1.ipynb",
                        "application/x-ipynb+json",
                        EMPTY_IPYNB.encode("utf-8"),
                        coding_id,
                    ),
                )

            async def _homework_questions() -> None:
                hw_q_id = await loop.run_in_executor(
                    None, partial(_create_folder, session, "3. Homework Questions", root_id)
                )
                await loop.run_in_executor(
                    None, partial(_create_blank_doc, session, f"{student_name} Topic 1 Homework", hw_q_id)
                )

            async def _homework_answers() -> None:
                hw_ans_id = await loop.run_in_executor(
                    None, partial(_create_folder, session, "4. Homework Sample Answers", root_id)
                )
                await loop.run_in_executor(
                    None,
                    partial(
                        _upload_multipart, session,
                        f"{student_name} Homework Topic 1.ipynb",
                        "application/x-ipynb+json",
                        EMPTY_IPYNB.encode("utf-8"),
                        hw_ans_id,
                    ),
                )

            async def _meet_doc() -> None:
                await loop.run_in_executor(
                    None, partial(_upload_html_as_doc, session, "Google Meet Link", html, root_id)
                )

            await asyncio.gather(
                _teaching_slides(),
                _coding_examples(),
                _homework_questions(),
                _homework_answers(),
                _meet_doc(),
            )
        else:
            await loop.run_in_executor(
                None, partial(_upload_html_as_doc, session, "Google Meet Link", html, root_id)
            )
    except Exception:
        try:
            await loop.run_in_executor(None, partial(_delete_file, session, root_id))
        except Exception:
            pass
        raise

    return f"https://drive.google.com/drive/folders/{root_id}"
