from supabase import AsyncClient, create_async_client

from app.config import settings

_client: AsyncClient | None = None


async def get_supabase() -> AsyncClient:
    global _client
    if _client is None:
        _client = await create_async_client(
            settings.supabase_url,
            settings.supabase_service_role_key,
        )
    return _client


async def get_setting(supabase: AsyncClient, key: str, default: str | None = None) -> str | None:
    result = (
        await supabase.from_("settings")
        .select("value")
        .eq("key", key)
        .maybe_single()
        .execute()
    )
    return result.data["value"] if (result and result.data) else default


async def get_active_students(supabase: AsyncClient, columns: str = "id, name, class_schedule") -> list[dict]:
    result = (
        await supabase.from_("students")
        .select(columns)
        .eq("status", "Active")
        .execute()
    )
    return result.data or []
