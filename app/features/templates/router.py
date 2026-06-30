"""Template CRUD endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import AsyncClient

from app.auth import require_internal_secret
from app.shared.db import get_supabase
from app.shared.response_models import OkResponse, TemplateItem

router = APIRouter(dependencies=[Depends(require_internal_secret)], tags=["templates"])


class TemplateUpdatePayload(BaseModel):
    content: str


@router.get("", response_model=list[TemplateItem])
async def list_templates(supabase: AsyncClient = Depends(get_supabase)):
    try:
        result = await supabase.from_("templates").select("id, content").order("id").execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result.data or []


@router.put("/{template_id}", response_model=OkResponse)
async def update_template(template_id: str, body: TemplateUpdatePayload, supabase: AsyncClient = Depends(get_supabase)):
    try:
        await supabase.from_("templates").upsert({"id": template_id, "content": body.content}, on_conflict="id").execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True}
