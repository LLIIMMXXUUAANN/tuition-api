from fastapi import Depends, HTTPException, Request

from app.config import settings


async def require_internal_secret(request: Request) -> None:
    secret = request.headers.get("X-Internal-Secret")
    if not secret or secret != settings.internal_api_secret:
        raise HTTPException(status_code=403, detail="Forbidden")


InternalAuth = Depends(require_internal_secret)
