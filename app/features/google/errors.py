def friendly_google_error(raw: str) -> str:
    if "invalid_grant" in raw:
        return "Google auth expired. Visit /api/google/auth to reconnect."
    if "insufficient" in raw.lower() or "403" in raw:
        return "Google API not authorised. Visit /api/google/auth to re-connect."
    return raw


def auth_expired(msg: str) -> bool:
    return "invalid_grant" in msg
