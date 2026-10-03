"""FR-CHT-01: a re-plan must respect the planner's constraints; propose_plan enforces them."""

from typing import Any

from services.shared.dynamo import to_item
from services.tools import case_tools
from services.tools.context import ToolContext
from services.tools.tests.test_tools import (  # noqa: F401 - pytest fixtures
    CASE,
    ctx,
    photo,
    reference_options,
)


def constrain(ctx: ToolContext, **limits: str) -> None:  # noqa: F811
    ctx.dynamodb.put_item(
        TableName="aera-test-cases",
        Item=to_item({"PK": f"CASE#{CASE}", "SK": "CONSTRAINTS", **limits}),
    )


def test_budget_and_exclusions_reject_the_reference_plan(ctx: ToolContext, photo: Any) -> None:  # noqa: F811
    options = reference_options(ctx, photo)
    constrain(ctx, maxCostUsd="30000", excludedActions="AIR_FREIGHT")

    result = case_tools.propose_plan(
        ctx, CASE, {"options": options, "chosen": ["C", "A"], "rationale": "C+A"}
    )

    assert result["accepted"] is False
    assert any("above the planner's budget" in e for e in result["errors"])
    assert any("excluded AIR_FREIGHT" in e for e in result["errors"])


def test_a_plan_within_the_constraints_is_accepted(ctx: ToolContext, photo: Any) -> None:  # noqa: F811
    options = reference_options(ctx, photo)
    constrain(ctx, maxCostUsd="30000", excludedActions="AIR_FREIGHT", needBy="2026-10-06")

    result = case_tools.propose_plan(
        ctx, CASE, {"options": options, "chosen": ["C"], "rationale": "C only"}
    )

    assert result["accepted"] is True and result["totalCostUsd"] == 4100
