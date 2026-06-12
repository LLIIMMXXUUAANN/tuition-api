import secrets
import time
import urllib.parse

import httpx
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

from app.config import settings

def _session(creds: Credentials) -> AuthorizedSession:
    """Requests session that bypasses system proxy (avoids httplib2 SSL issues on Windows)."""
    s = AuthorizedSession(creds)
    s.trust_env = False
    return s


# --- OAuth state tokens (CSRF protection) ---

_pending_states: dict[str, float] = {}  # token → expiry (monotonic)


def generate_state_token() -> str:
    token = secrets.token_urlsafe(32)
    _pending_states[token] = time.monotonic() + 600  # 10-minute TTL
    return token


def verify_and_consume_state(token: str) -> None:
    """Verify and delete the state token. Raises ValueError if invalid or expired."""
    expiry = _pending_states.pop(token, None)
    if expiry is None or time.monotonic() > expiry:
        raise ValueError("Invalid or expired state token.")

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]


def build_google_auth_url(state: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


async def exchange_code_for_refresh_token(code: str) -> str:
    """Exchange an auth code for a refresh token."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            GOOGLE_TOKEN_URI,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        r.raise_for_status()
        token = r.json().get("refresh_token")
        if not token:
            raise ValueError(
                "No refresh token returned. Revoke app access in Google Account settings and try again."
            )
        return token


async def get_oauth2_credentials(supabase) -> tuple[Credentials, str]:
    """Returns (credentials, original_refresh_token) — caller should check for rotation after ops."""
    result = (
        await supabase.from_("settings")
        .select("value")
        .eq("key", "google_refresh_token")
        .maybe_single()
        .execute()
    )
    refresh_token = result.data["value"] if (result and result.data) else None
    if not refresh_token:
        raise RuntimeError("Google not connected. Visit /api/google/auth to connect.")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
    )
    return creds, refresh_token


async def save_token_if_rotated(creds: Credentials, original_token: str, supabase) -> None:
    """Persist a rotated refresh token to DB. Non-fatal if the save fails."""
    new_token = creds.refresh_token
    if not new_token or new_token == original_token:
        return
    try:
        await supabase.from_("settings").upsert(
            {"key": "google_refresh_token", "value": new_token},
            on_conflict="key",
        ).execute()
    except Exception:
        pass
