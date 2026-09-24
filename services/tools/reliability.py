"""Read SAP-derived supplier performance for option risk calculations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from services.dialogue.reliability_job import read_profile
from services.tools.context import ToolContext, ToolError, json_dict


def profile_for(ctx: ToolContext, supplier_id: str, material: str) -> dict[str, Any] | None:
    profile = read_profile(ctx.dynamodb, supplier_id, material, ctx.env)
    if profile is None:
        return None
    computed = datetime.fromisoformat(str(profile["computedAt"]))
    if (computed.tzinfo is None or ctx.now() - computed > timedelta(hours=48)
            or computed > ctx.now() + timedelta(minutes=5)):
        return None
    if profile["sampleSize"] <= 0 or not profile.get("sourceRefs"):
        return None
    return profile


def get_supplier_reliability(
    ctx: ToolContext, supplier_id: str, material: str
) -> dict[str, Any]:
    if not supplier_id or not material:
        raise ToolError("supplierId and material are required")
    profile = profile_for(ctx, supplier_id, material)
    if profile is None:
        return {"supplierId": supplier_id, "material": material, "status": "NO_RECENT_SAP_HISTORY"}
    return json_dict({**profile, "status": "AVAILABLE"})
