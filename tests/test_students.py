"""Student + template CRUD — create/update/delete cycle against real Supabase."""

import uuid

import pytest

from app.features.students.service import build_insert_data, hash_payload


@pytest.fixture
def test_student_id(client, auth_headers):
    """Create a test student, yield its ID, then delete it."""
    r = client.post(
        "/students",
        json={"name": "pytest-test-student", "mode": "Other Syllabus", "fee_per_hour": 50.0},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    student_id = r.json()["id"]
    yield student_id
    # Cleanup — ignore errors if already deleted by the test
    client.delete(f"/students/{student_id}", headers=auth_headers)


def test_create_student(client, auth_headers):
    r = client.post(
        "/students",
        json={"name": "pytest-create-only", "mode": "Other Syllabus", "fee_per_hour": 40.0},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert "id" in data
    # Clean up
    client.delete(f"/students/{data['id']}", headers=auth_headers)


def test_create_student_missing_name(client, auth_headers):
    r = client.post(
        "/students",
        json={"mode": "Other Syllabus", "fee_per_hour": 50.0},
        headers=auth_headers,
    )
    assert r.status_code == 422


def test_update_student(client, auth_headers, test_student_id):
    r = client.put(
        f"/students/{test_student_id}",
        json={"fee_per_hour": 75.0},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "google_warning": None}


def test_update_student_empty_body(client, auth_headers, test_student_id):
    r = client.put(f"/students/{test_student_id}", json={}, headers=auth_headers)
    assert r.status_code == 400


def test_delete_student(client, auth_headers):
    # Create a dedicated student to delete
    r = client.post(
        "/students",
        json={"name": "pytest-delete-me", "mode": "Other Syllabus", "fee_per_hour": 30.0},
        headers=auth_headers,
    )
    assert r.status_code == 201
    sid = r.json()["id"]

    r = client.delete(f"/students/{sid}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "google_warning": None}


def test_update_template_and_restore(client, auth_headers):
    from supabase import create_client
    from app.config import settings

    sb = create_client(settings.supabase_url, settings.supabase_service_role_key)
    res = sb.from_("templates").select("content").eq("id", "payment").maybe_single().execute()
    original = (res.data["content"] if res and res.data else "") if res else ""

    try:
        r = client.put(
            "/templates/payment",
            json={"content": "pytest test content"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
    finally:
        client.put("/templates/payment", json={"content": original}, headers=auth_headers)


def test_create_student_idempotent_replay(client, auth_headers):
    key = f"pytest-idem-{uuid.uuid4()}"
    body = {"name": "pytest-idem-student", "mode": "Other Syllabus", "fee_per_hour": 45.0}
    headers = {**auth_headers, "Idempotency-Key": key}
    r1 = client.post("/students", json=body, headers=headers)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/students", json=body, headers=headers)
    assert r2.status_code == 201, r2.text
    assert r1.json()["id"] == r2.json()["id"]
    client.delete(f"/students/{r1.json()['id']}", headers=auth_headers)


def test_create_student_idempotent_payload_mismatch(client, auth_headers):
    key = f"pytest-idem-{uuid.uuid4()}"
    headers = {**auth_headers, "Idempotency-Key": key}
    body1 = {"name": "pytest-idem-a", "mode": "Other Syllabus", "fee_per_hour": 45.0}
    body2 = {"name": "pytest-idem-b", "mode": "Other Syllabus", "fee_per_hour": 45.0}
    r1 = client.post("/students", json=body1, headers=headers)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/students", json=body2, headers=headers)
    assert r2.status_code == 422
    client.delete(f"/students/{r1.json()['id']}", headers=auth_headers)


def test_create_student_without_idempotency_key_unaffected(client, auth_headers):
    r = client.post(
        "/students",
        json={"name": "pytest-no-idem-key", "mode": "Other Syllabus", "fee_per_hour": 45.0},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    client.delete(f"/students/{r.json()['id']}", headers=auth_headers)


def test_create_student_idempotency_conflict(client, auth_headers):
    from datetime import datetime, timedelta, timezone

    from supabase import create_client

    from app.config import settings

    sb = create_client(settings.supabase_url, settings.supabase_service_role_key)

    key = f"pytest-idem-conflict-{uuid.uuid4()}"
    body = {"name": "pytest-idem-conflict", "mode": "Other Syllabus", "fee_per_hour": 45.0}
    request_hash = hash_payload(build_insert_data(body))
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    try:
        sb.from_("idempotency_keys").insert({
            "key": key, "endpoint": "POST /students", "request_hash": request_hash,
            "status": "pending", "expires_at": expires_at,
        }).execute()

        headers = {**auth_headers, "Idempotency-Key": key}
        r = client.post("/students", json=body, headers=headers)
        assert r.status_code == 409
    finally:
        sb.from_("idempotency_keys").delete().eq("key", key).execute()
