"""Agent endpoints — smoke tests only: verify SSE stream opens and stop endpoint works."""

import json


def _read_first_sse_event(response) -> dict | None:
    """Read the first data line from an SSE response and parse it."""
    for line in response.iter_lines():
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload:
                return json.loads(payload)
    return None


def test_agent_stop_returns_ok(client, auth_headers):
    r = client.post(
        "/agent/stop",
        json={"requestId": "test-request-id"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_agent_chat_opens_sse_stream(client, auth_headers):
    with client.stream(
        "POST",
        "/agent/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "requestId": "test-smoke"},
        headers=auth_headers,
        timeout=30,
    ) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        # Read just enough to confirm SSE events are flowing
        event = _read_first_sse_event(r)
        assert event is not None
        assert "type" in event


def test_agent_lg_chat_opens_sse_stream(client, auth_headers):
    with client.stream(
        "POST",
        "/agent/lg/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "requestId": "test-lg-smoke"},
        headers=auth_headers,
        timeout=30,
    ) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        event = _read_first_sse_event(r)
        assert event is not None
        assert "type" in event
