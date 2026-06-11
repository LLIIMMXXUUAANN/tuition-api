import urllib.parse

import httpx
from google.oauth2.credentials import Credentials

from app.config import settings

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]


def build_google_auth_url() -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
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


async def get_oauth2_credentials(supabase) -> Credentials:
    """Reads the stored refresh token from Supabase and returns OAuth2 credentials."""
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

    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
    )
