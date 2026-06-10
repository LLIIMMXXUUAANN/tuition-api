"""Template CRUD endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import require_internal_secret
from app.services.supabase_client import get_supabase

router = APIRouter(dependencies=[Depends(require_internal_secret)])


class TemplateUpdatePayload(BaseModel):
    content: str


@router.get("")
async def list_templates():
    supabase = await get_supabase()
    result = await supabase.from_("templates").select("id, content").order("id").execute()
    return result.data or []


@router.put("/{template_id}")
async def update_template(template_id: str, body: TemplateUpdatePayload):
    supabase = await get_supabase()
    result = (
        await supabase.from_("templates")
        .upsert({"id": template_id, "content": body.content}, on_conflict="id")
        .execute()
    )
    if hasattr(result, "error") and result.error:
        raise HTTPException(status_code=400, detail=result.error.message)
    return {"ok": True}
