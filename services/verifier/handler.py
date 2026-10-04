"""Verifier Lambda: `PlanProposed` from the bus (SRD 6.6, 6.17)."""

from __future__ import annotations

import os
from typing import Any

from services.optimizer.runtime import lambda_solver
from services.verifier.logic import Grounding
from services.verifier.service import VerifierService

_service: VerifierService | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:

    global _service
    if _service is None:
        from services.shared import runtime
        from services.verifier.automated_reasoning import assess
        from services.verifier.grounding import evaluate

        bedrock = runtime.client("bedrock-runtime")

        def grounding(rationale: str, source: str, query: str) -> Grounding:
            return evaluate(
                bedrock,
                runtime.parameter("GROUNDING_GUARDRAIL_ID"),
                runtime.parameter("GROUNDING_GUARDRAIL_VERSION"),
                rationale=rationale,
                source=source,
                query=query,
            )

        _service = VerifierService(
            dynamodb=runtime.client("dynamodb"),
            sap=runtime.sap_client(),
            bus=runtime.client("events"),
            grounding=grounding,
            reasoning=lambda verified, proposed, policy: assess(
                bedrock,
                runtime.parameter("REASONING_GUARDRAIL_ID"),
                runtime.parameter("REASONING_GUARDRAIL_VERSION"),
                runtime.parameter("REASONING_POLICY_ARN"),
                verified,
                proposed,
                policy,
            ),
            scheduler=runtime.client("scheduler"),
            timer_target_arn=os.environ.get("AERA_APPROVAL_TIMER_ARN", ""),
            scheduler_role_arn=os.environ.get("AERA_SCHEDULER_ROLE_ARN", ""),
            env=runtime.env(),
            # BR-20: the portfolio solver runs in its own Lambda container (SRD 6.11).
            solver=(
                lambda_solver(runtime.client("lambda"), os.environ["AERA_OPTIMIZER_FUNCTION"])
                if os.environ.get("AERA_OPTIMIZER_FUNCTION")
                else None
            ),
        )
    data = (event.get("detail") or {}).get("data") or {}
    return _service.handle(str(data["caseId"]), int(data["planVersion"]))
