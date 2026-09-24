"""Run the supervisor on one case from this machine (SRD 6.18 "local development").

Same prompt, hooks and tools as the deployed runtime; tools run in process against the
environment's tables and SAP. The case is claimed and moved to INVESTIGATING exactly as
run-starter would, so a local run is indistinguishable in the trace.

    AERA_ENV=dev uv run --locked python scripts/agent_local.py --case EXC-2026-0914 --profile <p>
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AERA supervisor locally.")
    parser.add_argument("--case", required=True)
    parser.add_argument("--profile", default=os.environ.get("AERA_AWS_PROFILE"))
    parser.add_argument("--reason", default="local run")
    args = parser.parse_args(argv)
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    os.environ.setdefault("AERA_ENV", "dev")

    from strands.models.bedrock import BedrockModel

    from services.agent.harness import Harness
    from services.agent.tools import local_tools
    from services.shared import runtime
    from services.shared.cases import CaseStore
    from services.shared.models import CaseStatus, new_ulid
    from services.shared.runs import RunStore
    from services.tools.context import ToolContext

    dynamodb = runtime.client("dynamodb")
    case = CaseStore(dynamodb).get(args.case)
    if case is None:
        print(f"{args.case} does not exist", file=sys.stderr)
        return 1
    run_id = new_ulid()
    if not RunStore(dynamodb).claim(args.case, run_id):
        print(f"{args.case} already has an active run", file=sys.stderr)
        return 1
    CaseStore(dynamodb).transition(
        args.case, CaseStatus.INVESTIGATING, actor="system", reason=args.reason, run_id=run_id
    )
    model = BedrockModel(
        model_id=runtime.parameter("MODEL_SUPERVISOR_ID"),
        guardrail_id=runtime.parameter("GUARDRAIL_ID"),
        guardrail_version=runtime.parameter("GUARDRAIL_VERSION"),
        temperature=0.0,
    )
    harness = Harness(
        ctx=ToolContext(sap=runtime.sap_client(), dynamodb=dynamodb, bus=runtime.client("events")),
        model=model,
        tools=local_tools,
    )
    outcome = harness.run(
        {"caseId": args.case, "runId": run_id, "mode": "investigate", "reason": args.reason}
    )
    print(f"{args.case} run {run_id}: {outcome.end_reason}")
    print(outcome.summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
