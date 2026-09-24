"""Verifier Lambda: `PlanProposed` from the bus (SRD 6.6, 6.17)."""

from __future__ import annotations

import os
from typing import Any

from services.verifier.logic import Grounding
from services.verifier.service import VerifierService

_service: VerifierService | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:

    global _service
    if _service is None:
        from services.shared import runtime
        from services.verifier.grounding import evaluate

        bedrock = runtime.client("bedrock-runtime")

        def grounding(rationale: str, source: str, query: str) -> Grounding:
            return evaluate(
                bedrock,
                runtime.parameter("GUARDRAIL_ID"),
                runtime.parameter("GUARDRAIL_VERSION"),
                rationale=rationale,
                source=source,
                query=query,
            )

        _service = VerifierService(
            dynamodb=runtime.client("dynamodb"),
            sap=runtime.sap_client(),
            bus=runtime.client("events"),
            grounding=grounding,
            scheduler=runtime.client("scheduler"),
            timer_target_arn=os.environ.get("AERA_APPROVAL_TIMER_ARN", ""),
            scheduler_role_arn=os.environ.get("AERA_SCHEDULER_ROLE_ARN", ""),
            env=runtime.env(),
        )
    data = (event.get("detail") or {}).get("data") or {}
    return _service.handle(str(data["caseId"]), int(data["planVersion"]))
