"""Auth guard: every endpoint must reject missing/wrong X-Internal-Secret."""

VALID_BODY = {
    "studentId": "00000000-0000-0000-0000-000000000000",
    "month": 6,
    "year": 2026,
    "templateType": 1,
}


def test_missing_secret_returns_403(client):
    r = client.post("/payment/generate", json=VALID_BODY)
    assert r.status_code == 403


def test_wrong_secret_returns_403(client):
    r = client.post(
        "/payment/generate",
        json=VALID_BODY,
        headers={"X-Internal-Secret": "wrong-secret"},
    )
    assert r.status_code == 403


def test_correct_secret_passes_auth(client, auth_headers):
    # Invalid body but auth passes → expect 404 or 400, NOT 403
    r = client.post("/payment/generate", json=VALID_BODY, headers=auth_headers)
    assert r.status_code != 403
