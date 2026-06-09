"""Payment message generation — integration tests against real Supabase."""

import pytest


@pytest.fixture(scope="module")
def active_student_id():
    """Fetch an active student ID from the real DB to use in payment tests."""
    from supabase import create_client
    from app.config import settings

    sb = create_client(settings.supabase_url, settings.supabase_service_role_key)
    result = (
        sb.from_("students")
        .select("id")
        .eq("status", "Active")
        .limit(1)
        .single()
        .execute()
    )
    return result.data["id"]


def test_generate_payment_success(client, auth_headers, active_student_id):
    r = client.post(
        "/payment/generate",
        json={"studentId": active_student_id, "month": 7, "year": 2026, "templateType": 1},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "message" in data
    assert len(data["message"]) > 0


def test_generate_payment_month_zero(client, auth_headers, active_student_id):
    r = client.post(
        "/payment/generate",
        json={"studentId": active_student_id, "month": 0, "year": 2026, "templateType": 1},
        headers=auth_headers,
    )
    assert r.status_code == 400


def test_generate_payment_month_13(client, auth_headers, active_student_id):
    r = client.post(
        "/payment/generate",
        json={"studentId": active_student_id, "month": 13, "year": 2026, "templateType": 1},
        headers=auth_headers,
    )
    assert r.status_code == 400


def test_generate_payment_bad_year(client, auth_headers, active_student_id):
    r = client.post(
        "/payment/generate",
        json={"studentId": active_student_id, "month": 6, "year": 2019, "templateType": 1},
        headers=auth_headers,
    )
    assert r.status_code == 400


def test_generate_payment_nonexistent_student(client, auth_headers):
    r = client.post(
        "/payment/generate",
        json={
            "studentId": "00000000-0000-0000-0000-000000000000",
            "month": 6,
            "year": 2026,
            "templateType": 1,
        },
        headers=auth_headers,
    )
    assert r.status_code == 404


def test_generate_payment_template2(client, auth_headers, active_student_id):
    r = client.post(
        "/payment/generate",
        json={
            "studentId": active_student_id,
            "month": 7,
            "year": 2026,
            "templateType": 2,
            "carryover": 50.0,
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert "message" in r.json()
