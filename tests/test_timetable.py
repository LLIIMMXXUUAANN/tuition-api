"""Timetable rules, buffer mins, and slot generation endpoints."""


def test_get_rules(client, auth_headers):
    r = client.get("/timetable/rules", headers=auth_headers)
    assert r.status_code == 200
    assert "rules" in r.json()


def test_update_and_restore_rules(client, auth_headers):
    original = client.get("/timetable/rules", headers=auth_headers).json()["rules"]
    try:
        r = client.post("/timetable/rules", json={"rules": "pytest test rule"}, headers=auth_headers)
        assert r.status_code == 200
        assert r.json() == {"ok": True}

        check = client.get("/timetable/rules", headers=auth_headers).json()["rules"]
        assert check == "pytest test rule"
    finally:
        client.post("/timetable/rules", json={"rules": original}, headers=auth_headers)


def test_get_buffer_mins(client, auth_headers):
    r = client.get("/timetable/buffer-mins", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert "buffer_mins" in data
    assert isinstance(data["buffer_mins"], int)
    assert 0 <= data["buffer_mins"] <= 60


def test_update_and_restore_buffer_mins(client, auth_headers):
    original = client.get("/timetable/buffer-mins", headers=auth_headers).json()["buffer_mins"]
    try:
        r = client.post("/timetable/buffer-mins", json={"bufferMins": 20}, headers=auth_headers)
        assert r.status_code == 200
        assert r.json() == {"ok": True}

        check = client.get("/timetable/buffer-mins", headers=auth_headers).json()["buffer_mins"]
        assert check == 20
    finally:
        client.post("/timetable/buffer-mins", json={"bufferMins": original}, headers=auth_headers)


def test_buffer_mins_out_of_range(client, auth_headers):
    r = client.post("/timetable/buffer-mins", json={"bufferMins": 61}, headers=auth_headers)
    assert r.status_code == 400


def test_buffer_mins_negative(client, auth_headers):
    r = client.post("/timetable/buffer-mins", json={"bufferMins": -1}, headers=auth_headers)
    assert r.status_code == 400


def test_generate_slots_no_rules(client, auth_headers):
    r = client.post(
        "/timetable/generate-slots",
        json={"rules": "", "bookedSlots": [], "bufferMins": 15},
        headers=auth_headers,
    )
    assert r.status_code == 400


def test_generate_slots_with_rules(client, auth_headers):
    rules = "Available Monday to Friday 14:00 to 22:00. Prefer afternoons."
    r = client.post(
        "/timetable/generate-slots",
        json={"rules": rules, "bookedSlots": [], "bufferMins": 15},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "slots" in data
    assert isinstance(data["slots"], list)
    # Each slot should have day, time, state
    if data["slots"]:
        slot = data["slots"][0]
        assert "day" in slot
        assert "time" in slot
        assert "state" in slot
