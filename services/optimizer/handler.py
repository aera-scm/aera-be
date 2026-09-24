"""Internal Lambda entrypoint for the portfolio solver (FR-OPZ-02, BR-20)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from services.optimizer.solver import Candidate, Capacity, Need, solve


def _whole(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("portfolio quantities and cents must be JSON integers")
    return value


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    needs = [
        Need(
            case_id=str(row["caseId"]),
            id=str(row["id"]),
            due=datetime.fromisoformat(str(row["due"]).replace("Z", "+00:00")),
            quantity=_whole(row["quantity"]),
            lost_revenue_cents_per_unit=_whole(row["lostRevenueCentsPerUnit"]),
            source_ref=str(row["sourceRef"]),
        )
        for row in event["needs"]
    ]
    candidates = [
        Candidate(
            case_id=str(row["caseId"]),
            id=str(row["id"]),
            arrival=datetime.fromisoformat(str(row["arrival"]).replace("Z", "+00:00")),
            max_quantity=_whole(row["maxQuantity"]),
            fixed_cost_cents=_whole(row["fixedCostCents"]),
            unit_cost_cents=_whole(row["unitCostCents"]),
            resources=tuple(str(key) for key in row["resources"]),
            source_ref=str(row["sourceRef"]),
            sizeable=row.get("sizeable", True),
        )
        for row in event["candidates"]
    ]
    capacities = [
        Capacity(str(row["resource"]), _whole(row["quantity"]), str(row["sourceRef"]))
        for row in event["capacities"]
    ]
    result = solve(needs, candidates, capacities)
    return {
        "status": result.status,
        "objectiveCents": result.objective_cents,
        "allocations": [
            {
                "candidateId": allocation.candidate_id,
                "caseId": allocation.case_id,
                "quantity": allocation.quantity,
                "covered": [{"needId": key, "quantity": qty} for key, qty in allocation.covered],
            }
            for allocation in result.allocations
        ],
        "uncovered": [{"needId": key, "quantity": qty} for key, qty in result.uncovered],
        "requiresVerification": result.requires_verification,
    }
