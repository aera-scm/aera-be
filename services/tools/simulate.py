"""`simulate_plan`: projected stock per plant for the baseline, each option and the options
together (FR-SIM-01, FR-SIM-02). Read-only: options are recalculated from their tool
parameters, nothing is recorded, and every figure keeps its source references."""

from __future__ import annotations

from typing import Any

from services.optimizer.inputs import CaseProjector
from services.optimizer.projection import Projection
from services.shared.models import Option
from services.tools.calc import compute_option
from services.tools.context import ToolContext, ToolError, json_dict

ACTION_TYPE = {
    "CREATE_STO": "STO",
    "BOOK_AIR_FREIGHT": "AIR_FREIGHT",
    "CREATE_PO_ALTERNATE": "ALTERNATE_SUPPLIER",
}


def summary(projections: dict[str, Projection]) -> dict[str, Any]:
    return {
        plant: {
            "firstStockout": p.first_stockout,
            "unitsShort": p.units_short,
            "lineStops": p.json()["lineStops"],
            "sourceRefs": sorted(set(p.source_refs)),
        }
        for plant, p in sorted(projections.items())
    }


def build_option(
    ctx: ToolContext, case_id: str, option_id: str, action_type: str, params: dict[str, Any]
) -> Option:
    draft, _ = compute_option(ctx, case_id, action_type, params)
    return Option.model_validate(
        {
            "id": option_id,
            "name": f"{action_type} {option_id}",
            "actions": draft["actions"],
            "coverageUnits": draft["coverageUnits"],
            "arrival": draft["arrival"],
            "costUsd": draft["costUsd"],
            "costSourceRef": draft["costSourceRef"],
            "figures": draft["figures"],
            "rationale": "simulation",
        }
    )


def simulate_plan(ctx: ToolContext, case_id: str, options: list[dict[str, Any]]) -> dict[str, Any]:
    case = ctx.cases.get(case_id)
    if case is None:
        raise ToolError(f"case {case_id} does not exist")
    if not isinstance(options, list) or not 1 <= len(options) <= 3:
        raise ToolError("options: one to three {id, actionType, params} objects")
    built = []
    for index, spec in enumerate(options):
        if not isinstance(spec, dict):
            raise ToolError("each option is an object {id, actionType, params}")
        option_id = str(spec.get("id") or "ABC"[index])
        built.append(
            build_option(
                ctx, case_id, option_id, str(spec.get("actionType")), dict(spec.get("params") or {})
            )
        )
    projector = CaseProjector(ctx, case)
    return json_dict(
        {
            "caseId": case_id,
            "baseline": summary(projector.projections([])),
            "options": {o.id: summary(projector.projections([o])) for o in built},
            "combined": summary(projector.projections(built)),
        }
    )
