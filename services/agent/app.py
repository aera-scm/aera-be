"""AgentCore Runtime entry point (SRD 6.18).

Returns at once; the run executes in a background task registered with `add_async_task` /
`complete_async_task`, so the health check reports HealthyBusy while it runs and the entry
point never blocks. Tools come from the AgentCore Gateway; model ids and the Guardrail from
SSM (`MODEL_SUPERVISOR_ID`, `GUARDRAIL_ID`, `GUARDRAIL_VERSION`).
"""

from __future__ import annotations

import os
import threading
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()


def build_harness() -> Any:
    from strands.models.bedrock import BedrockModel

    from services.agent.harness import Harness
    from services.agent.hooks import Limits
    from services.agent.tools import gateway_client
    from services.shared import runtime
    from services.shared.config import Config
    from services.tools.context import ToolContext

    dynamodb = runtime.client("dynamodb")
    config = Config(dynamodb)
    region = os.environ.get("AWS_REGION", "us-east-1")
    model = BedrockModel(
        model_id=runtime.parameter("MODEL_SUPERVISOR_ID"),
        region_name=region,
        guardrail_id=runtime.parameter("GUARDRAIL_ID"),
        guardrail_version=runtime.parameter("GUARDRAIL_VERSION"),
        guardrail_trace="enabled",
        temperature=0.0,
    )
    gateway = gateway_client(os.environ["AERA_GATEWAY_URL"], region)
    return Harness(
        ctx=ToolContext(sap=runtime.sap_client(), dynamodb=dynamodb, bus=runtime.client("events")),
        model=model,
        tools=lambda ctx: [gateway],
        limits=Limits(
            max_iterations=int(config.decimal("MAX_ITERATIONS")),
            max_tokens=int(config.decimal("MAX_TOKENS")),
            max_seconds=float(config.decimal("MAX_RUN_SECONDS")),
        ),
    )


def _run(payload: dict[str, Any], task_id: int) -> None:
    try:
        build_harness().run(payload)
    finally:
        app.complete_async_task(task_id)


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload.get("caseId") or not payload.get("runId"):
        return {"status": "rejected", "reason": "caseId and runId are required"}
    task_id = app.add_async_task("case_run", {"caseId": payload["caseId"]})
    threading.Thread(target=_run, args=(payload, task_id), daemon=True).start()
    return {"status": "started", "runId": payload["runId"]}


if __name__ == "__main__":
    app.run()
