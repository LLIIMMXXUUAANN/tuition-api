from google.oauth2.credentials import Credentials

from app.config import settings


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
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
    )
