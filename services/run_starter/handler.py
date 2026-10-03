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

from services.routing.store import ControlStore
from services.shared.case_state import can_transition
from services.shared.cases import CaseStore, ConcurrentUpdateError, IllegalTransitionError
from services.shared.dynamo import from_item, table_name, to_item
from services.shared.models import CaseStatus, EventType, new_ulid
from services.shared.observability import get_logger
from services.shared.runs import RunStore
from services.shared.runtime import emit

COMPONENT = "run-starter"
STARTABLE = (
    CaseStatus.TRIAGED,
    CaseStatus.WAITING_PLANNER,
    CaseStatus.WAITING_SUPPLIER,
    # Re-planning (FR-CHT-01): a proposed, rejected or escalated plan may be redone.
    CaseStatus.PLAN_PROPOSED,
    CaseStatus.REJECTED,
    CaseStatus.ESCALATED,
)
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
        self,
        case_id: str,
        *,
        reason: str,
        mode: str = "investigate",
        actor: str = "system",
        run_id: str | None = None,
        constraints: dict[str, str] | None = None,
    ) -> Started:
        case = self.cases.get(case_id)
        for pending in self._pending_events(case_id):
            self._publish_event(case_id, pending)
            if pending["eventType"] == "RunStarted" and (
                run_id == pending["data"]["runId"]
                or (
                    run_id is None
                    and case is not None
                    and case.active_run_id == pending["data"]["runId"]
                )
            ):
                return Started(str(pending["data"]["runId"]), "started")
        if case is None:
            return Started(None, "case not found")
        # BR-23 (ADR-0036): an approval whose deadline passed is re-planned and re-verified.
        reverify = case.status is CaseStatus.AWAITING_APPROVAL and ControlStore(
            self.dynamodb, self.env
        ).expired_pending(case_id, case.plan_version)
        if (case.status not in STARTABLE and not reverify) or not can_transition(
            case.status, CaseStatus.INVESTIGATING
        ):
            return Started(None, f"case is {case.status.value}")
        run_id = run_id or new_ulid()
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
        payload: dict[str, Any] = {
            "mode": mode,
            "caseId": case_id,
            "runId": run_id,
            "reason": reason,
            "constraints": constraints or {},
        }
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
            current = self.cases.get(case_id)
            if current is not None and current.status is CaseStatus.INVESTIGATING:
                self.cases.transition(
                    case_id,
                    CaseStatus.ESCALATED
                    if self.runs.consecutive_failures(case_id) >= 1
                    else CaseStatus.WAITING_PLANNER,
                    actor="system",
                    reason="runtime not reachable",
                    expected=CaseStatus.INVESTIGATING,
                    run_id=run_id,
                )
            self.runs.release(
                case_id, run_id, end_reason="LIMIT_ERROR", summary="runtime not reachable"
            )
            self._event(
                case_id,
                "RunEnded",
                {"caseId": case_id, "runId": run_id, "endReason": "LIMIT_ERROR"},
            )
            return Started(None, "runtime not reachable")
        self._event(
            case_id,
            "RunStarted",
            {"caseId": case_id, "runId": run_id, "reason": reason, "mode": mode},
            actor=actor,
        )
        return Started(run_id, "started")

    def _event(
        self, case_id: str, event_type: EventType, data: dict[str, Any], *, actor: str = "system"
    ) -> None:
        pending = {"eventType": event_type, "data": data, "actor": actor}
        self.dynamodb.put_item(
            TableName=table_name("cases", self.env),
            Item=to_item(
                {"PK": f"CASE#{case_id}", "SK": f"START_EVENT#{data['runId']}", **pending}
            ),
        )
        self._publish_event(case_id, pending)

    def _pending_events(self, case_id: str) -> list[dict[str, Any]]:
        arguments: dict[str, Any] = {
            "TableName": table_name("cases", self.env),
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :event)",
            "ExpressionAttributeValues": {
                ":pk": {"S": f"CASE#{case_id}"},
                ":event": {"S": "START_EVENT#"},
            },
            "ConsistentRead": True,
        }
        pending: list[dict[str, Any]] = []
        while True:
            page = self.dynamodb.query(**arguments)
            pending.extend(from_item(item, keep_decimals=False) for item in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return pending
            arguments["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def _publish_event(self, case_id: str, pending: dict[str, Any]) -> None:
        emit(
            self.bus,
            pending["eventType"],
            pending["data"],
            component=COMPONENT,
            case_id=case_id,
            run_id=pending["data"]["runId"],
            actor=pending["actor"],
            environment=self.env,
        )
        try:
            self.dynamodb.delete_item(
                TableName=table_name("cases", self.env),
                Key={
                    "PK": {"S": f"CASE#{case_id}"},
                    "SK": {"S": f"START_EVENT#{pending['data']['runId']}"},
                },
                ConditionExpression="#data.runId = :run",
                ExpressionAttributeNames={"#data": "data"},
                ExpressionAttributeValues={":run": {"S": pending["data"]["runId"]}},
            )
        except self.dynamodb.exceptions.ConditionalCheckFailedException:
            pass  # another delivery acknowledged it, or a newer run has an event pending


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
    started = _starter.start(
        str(data["caseId"]),
        reason=str(data.get("reason") or "ready"),
        run_id=data.get("runId"),
        mode=str(data.get("mode") or "investigate"),
        constraints=dict(data.get("constraints") or {}),
        actor=str(event.get("detail", {}).get("actor") or "system"),
    )
    return {"runId": started.run_id, "reason": started.reason}
