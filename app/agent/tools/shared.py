from supabase import AsyncClient

SupabaseClient = AsyncClient


def err_msg(err: Exception | BaseException | None, fallback: str = "Unknown error") -> str:
    if isinstance(err, Exception) and str(err):
        return str(err)
    return fallback
