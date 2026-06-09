"""Self-evaluation after mutations — port of src/features/agent/lib/eval.ts."""

from __future__ import annotations

from typing import Any

from supabase import AsyncClient


async def self_eval(
    tool_name: str,
    args: dict[str, Any],
    supabase: AsyncClient,
    created_id: str | None = None,
) -> str:
    """Verify a mutation against the DB and return a short verdict string."""
    try:
        if tool_name in ("create_student", "update_student"):
            id_ = created_id if tool_name == "create_student" else args.get("id")
            if not id_:
                return "⚠ could not verify"
            result = (
                await supabase.from_("students")
                .select("id")
                .eq("id", id_)
                .maybe_single()
                .execute()
            )
            return "✓ verified in DB" if (result and result.data) else "⚠ could not verify"

        if tool_name == "delete_student":
            result = (
                await supabase.from_("students")
                .select("id")
                .eq("id", args["id"])
                .maybe_single()
                .execute()
            )
            return "✓ verified deleted" if not (result and result.data) else "_⚠ student still exists in DB_"

        if tool_name == "setup_student_google":
            result = (
                await supabase.from_("students")
                .select("google_meet_link, google_drive_link")
                .eq("id", args["student_id"])
                .maybe_single()
                .execute()
            )
            if not (result and result.data):
                return "⚠ could not verify"
            data = result.data
            parts = [
                "✓ Meet link set" if data.get("google_meet_link") else "⚠ Meet link missing",
                "✓ Drive folder set" if data.get("google_drive_link") else "⚠ Drive folder missing",
            ]
            return ", ".join(parts)

        if tool_name == "update_timetable_rules":
            result = (
                await supabase.from_("settings")
                .select("value")
                .eq("key", "timetable_rules")
                .maybe_single()
                .execute()
            )
            if result and result.data and result.data.get("value") == args.get("rules"):
                return "✓ rules verified in DB"
            return "⚠ could not verify rules"

        if tool_name == "update_buffer_mins":
            result = (
                await supabase.from_("settings")
                .select("value")
                .eq("key", "timetable_buffer_mins")
                .maybe_single()
                .execute()
            )
            if result and result.data:
                try:
                    stored = int(result.data.get("value", ""))
                    if stored == int(args["buffer_mins"]):
                        return f"✓ buffer set to {args['buffer_mins']}m"
                except (ValueError, TypeError):
                    pass
            return "⚠ could not verify buffer"

    except Exception:
        return "⚠ could not verify"

    return ""
