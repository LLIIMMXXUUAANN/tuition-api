"""Student + template CRUD — create/update/delete cycle against real Supabase."""

import pytest


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
    assert r.json() == {"ok": True}


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
    assert r.json() == {"ok": True}


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
