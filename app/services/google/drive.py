import asyncio
import io
import json
import re

from googleapiclient.http import MediaIoBaseUpload
from google.oauth2.credentials import Credentials

from app.config import settings
from app.services.google.auth import build_drive
from app.types import ClassSlot

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


async def update_student_meet_doc(
    creds: Credentials,
    drive_folder_url: str,
    student_name: str,
    schedule: list[ClassSlot],
    meet_link: str,
) -> None:
    """Finds the 'Google Meet Link' doc in the student's Drive folder and rewrites its HTML."""
    folder_id = parse_drive_folder_id(drive_folder_url)
    loop = asyncio.get_event_loop()
    service = build_drive(creds)

    search_res = await loop.run_in_executor(
        None,
        lambda: service.files()
        .list(
            q=(
                f"'{folder_id}' in parents "
                "and name = 'Google Meet Link' "
                "and mimeType = 'application/vnd.google-apps.document' "
                "and trashed = false"
            ),
            fields="files(id)",
            pageSize=1,
        )
        .execute(),
    )
    files = search_res.get("files", [])
    if not files:
        raise RuntimeError("Google Meet Link doc not found in student Drive folder")
    doc_id = files[0]["id"]

    html_content = build_meet_doc_html(student_name, schedule, meet_link)
    media = MediaIoBaseUpload(
        io.BytesIO(html_content.encode("utf-8")), mimetype="text/html"
    )

    await loop.run_in_executor(
        None,
        lambda: service.files()
        .update(fileId=doc_id, body={}, media_body=media)
        .execute(),
    )


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

    loop = asyncio.get_event_loop()
    service = build_drive(creds)

    # --- helpers ----------------------------------------------------------------

    async def _create_folder(name: str, parent_id: str) -> str:
        res = await loop.run_in_executor(
            None,
            lambda: service.files()
            .create(
                body={
                    "name": name,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent_id],
                },
                fields="id",
            )
            .execute(),
        )
        return res["id"]

    async def _create_shortcut(target_id: str, parent_id: str, name: str) -> None:
        await loop.run_in_executor(
            None,
            lambda: service.files()
            .create(
                body={
                    "name": name,
                    "mimeType": "application/vnd.google-apps.shortcut",
                    "parents": [parent_id],
                    "shortcutDetails": {"targetId": target_id},
                }
            )
            .execute(),
        )

    async def _upload_ipynb(name: str, parent_id: str) -> None:
        media = MediaIoBaseUpload(
            io.BytesIO(EMPTY_IPYNB.encode("utf-8")),
            mimetype="application/x-ipynb+json",
        )
        await loop.run_in_executor(
            None,
            lambda: service.files()
            .create(
                body={"name": f"{name}.ipynb", "parents": [parent_id]},
                media_body=media,
                fields="id",
            )
            .execute(),
        )

    async def _create_blank_doc(name: str, parent_id: str) -> None:
        await loop.run_in_executor(
            None,
            lambda: service.files()
            .create(
                body={
                    "name": name,
                    "mimeType": "application/vnd.google-apps.document",
                    "parents": [parent_id],
                }
            )
            .execute(),
        )

    async def _create_meet_doc(parent_id: str) -> None:
        html_content = build_meet_doc_html(student_name, class_schedule, meet_link)
        media = MediaIoBaseUpload(
            io.BytesIO(html_content.encode("utf-8")), mimetype="text/html"
        )
        await loop.run_in_executor(
            None,
            lambda: service.files()
            .create(
                body={
                    "name": "Google Meet Link",
                    "mimeType": "application/vnd.google-apps.document",
                    "parents": [parent_id],
                },
                media_body=media,
                fields="id",
            )
            .execute(),
        )

    # --- create root folder -----------------------------------------------------

    root_id = await _create_folder(student_name, students_folder_id)

    await loop.run_in_executor(
        None,
        lambda: service.permissions()
        .create(
            fileId=root_id,
            body={"role": "reader", "type": "anyone"},
        )
        .execute(),
    )

    # --- create contents --------------------------------------------------------

    try:
        if mode == "My Python Syllabus":

            async def _teaching_slides() -> None:
                teaching_id = await _create_folder("1. Teaching Slides", root_id)
                await _create_shortcut(lec_topic1_file_id, teaching_id, "Topic_1.pptx")

            async def _coding_examples() -> None:
                coding_id = await _create_folder(
                    "2. In-Class Coding Examples", root_id
                )
                await _upload_ipynb(f"{student_name} Topic 1", coding_id)

            async def _homework_questions() -> None:
                hw_q_id = await _create_folder("3. Homework Questions", root_id)
                await _create_blank_doc(f"{student_name} Topic 1 Homework", hw_q_id)

            async def _homework_answers() -> None:
                hw_ans_id = await _create_folder(
                    "4. Homework Sample Answers", root_id
                )
                await _upload_ipynb(f"{student_name} Homework Topic 1", hw_ans_id)

            await asyncio.gather(
                _teaching_slides(),
                _coding_examples(),
                _homework_questions(),
                _homework_answers(),
                _create_meet_doc(root_id),
            )
        else:
            await _create_meet_doc(root_id)
    except Exception:
        # Clean up root folder on any failure so a retry doesn't create duplicates
        try:
            await loop.run_in_executor(
                None,
                lambda: service.files().delete(fileId=root_id).execute(),
            )
        except Exception:
            pass
        raise

    return f"https://drive.google.com/drive/folders/{root_id}"
