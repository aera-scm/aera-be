"""Run-starter: start agent runs asynchronously, one active run per case (SRD 6.17, 6.18,
NFR-REL-04).

Triggered by `CaseReadyForRun` and by `POST /cases/{id}/runs`. It claims the case with a
conditional write on `activeRunId`, moves it to INVESTIGATING, and invokes AgentCore Runtime,
whose entry point returns at once; the run id is also the runtime session id. If the runtime
cannot be reached the claim is released and the failure counts toward escalation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.shared.case_state import can_transition
from services.shared.cases import CaseStore, ConcurrentUpdateError, IllegalTransitionError
from services.shared.models import CaseStatus, new_ulid
from services.shared.observability import get_logger
from services.shared.runs import RunStore
from services.shared.runtime import emit

COMPONENT = "run-starter"
STARTABLE = (CaseStatus.TRIAGED, CaseStatus.WAITING_PLANNER, CaseStatus.WAITING_SUPPLIER)
_log = get_logger("run-starter")


@dataclass
class Started:
    run_id: str | None
    reason: str


@dataclass
class RunStarter:
    dynamodb: Any
    agentcore: Any
    runtime_arn: Callable[[], str]
    bus: Any
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    env: str | None = None

    def __post_init__(self) -> None:
        self.cases = CaseStore(self.dynamodb, self.env, clock=self.clock)
        self.runs = RunStore(self.dynamodb, self.env, clock=self.clock)

    def start(
        self, case_id: str, *, reason: str, mode: str = "investigate", actor: str = "system"
    ) -> Started:
        case = self.cases.get(case_id)
        if case is None:
            return Started(None, "case not found")
        if case.status not in STARTABLE or not can_transition(
            case.status, CaseStatus.INVESTIGATING
        ):
            return Started(None, f"case is {case.status.value}")
        run_id = new_ulid()
        if not self.runs.claim(case_id, run_id):
            return Started(None, "a run is already active")
        try:
            self.cases.transition(
                case_id,
                CaseStatus.INVESTIGATING,
                actor=actor,
                reason=reason,
                expected=case.status,
                run_id=run_id,
            )
        except (IllegalTransitionError, ConcurrentUpdateError):
            self.runs.release(case_id, run_id, end_reason="LIMIT_ERROR", summary="not started")
            return Started(None, "case changed while starting")
        payload = {"mode": mode, "caseId": case_id, "runId": run_id, "reason": reason}
        try:
            self.agentcore.invoke_agent_runtime(
                agentRuntimeArn=self.runtime_arn(),
                runtimeSessionId=f"{case_id}-{run_id}",
                payload=json.dumps(payload).encode(),
            )
        except Exception as error:  # noqa: BLE001 - any failure to reach the runtime
            _log.warning(
                "runtime invocation failed",
                extra={"caseId": case_id, "runId": run_id, "error": type(error).__name__},
            )
            self.runs.release(
                case_id, run_id, end_reason="LIMIT_ERROR", summary="runtime not reachable"
            )
            emit(
                self.bus,
                "RunEnded",
                {"caseId": case_id, "runId": run_id, "endReason": "LIMIT_ERROR"},
                component=COMPONENT,
                case_id=case_id,
                run_id=run_id,
                environment=self.env,
            )
            return Started(None, "runtime not reachable")
        emit(
            self.bus,
            "RunStarted",
            {"caseId": case_id, "runId": run_id, "reason": reason, "mode": mode},
            component=COMPONENT,
            case_id=case_id,
            run_id=run_id,
            actor=actor,
            environment=self.env,
        )
        return Started(run_id, "started")


_starter: RunStarter | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _starter
    if _starter is None:
        import os

        from services.shared import runtime

        _starter = RunStarter(
            dynamodb=runtime.client("dynamodb"),
            agentcore=runtime.client("bedrock-agentcore"),
            runtime_arn=lambda: os.environ["AERA_AGENT_RUNTIME_ARN"],
            bus=runtime.client("events"),
        )
    data = (event.get("detail") or {}).get("data") or {}
    started = _starter.start(str(data["caseId"]), reason=str(data.get("reason") or "ready"))
    return {"runId": started.run_id, "reason": started.reason}
