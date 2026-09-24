"""Execution workflow definition (SRD 6.8) as Amazon States Language.

State names follow 6.8. `Execute` performs RevalidatePlan, ReserveStock, SaveUndoPlan and the
sequential per-action writes (check idempotency, write, record, verify) with compensation
inside one task, because the durable write journal — not the state machine — is what makes
each write exactly-once and each undo ordered (ADR-0016). Every task is idempotent, so the
retries here are safe.
"""

from __future__ import annotations

from typing import Any

LAMBDA_ERRORS = [
    "Lambda.ServiceException",
    "Lambda.AWSLambdaException",
    "Lambda.SdkClientException",
    "Lambda.TooManyRequestsException",
]


def _task(
    function_arn: str, step: str, next_state: str | None, extra: dict[str, str] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"step": step, "caseId.$": "$.caseId"}
    if step != "Rollback":
        payload["planPartId.$"] = "$.planPartId"
    payload.update(extra or {})
    state: dict[str, Any] = {
        "Type": "Task",
        "Resource": "arn:aws:states:::lambda:invoke",
        "Parameters": {"FunctionName": function_arn, "Payload": payload},
        "ResultSelector": {"value.$": "$.Payload"},
        "ResultPath": f"$.{step[0].lower()}{step[1:]}",
        "Retry": [
            {
                "ErrorEquals": LAMBDA_ERRORS,
                "IntervalSeconds": 2,
                "MaxAttempts": 3,
                "BackoffRate": 2.0,
            }
        ],
    }
    if next_state is None:
        state["End"] = True
    else:
        state["Next"] = next_state
    return state


def definition(function_arn: str) -> dict[str, Any]:
    execute = _task(function_arn, "Execute", "Outcome")
    execute["Catch"] = [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "Refused"}]
    rollback = _task(function_arn, "Rollback", "RolledBack")
    rollback["Catch"] = [
        {"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "RollbackFailed"}
    ]
    return {
        "Comment": "AERA governed execution of an approved plan part (SRD 6.8)",
        "StartAt": "Mode",
        "States": {
            "Mode": {
                "Type": "Choice",
                "Choices": [{"Variable": "$.mode", "StringEquals": "rollback", "Next": "Rollback"}],
                "Default": "CheckKillSwitch",
            },
            "CheckKillSwitch": _task(function_arn, "CheckKillSwitch", "KillSwitchOn"),
            "KillSwitchOn": {
                "Type": "Choice",
                "Choices": [
                    {
                        "Variable": "$.checkKillSwitch.value.killed",
                        "BooleanEquals": True,
                        "Next": "Refused",
                    }
                ],
                "Default": "Execute",
            },
            "Execute": execute,
            "Outcome": {
                "Type": "Choice",
                "Choices": [
                    {
                        "Variable": "$.execute.value.outcome",
                        "StringEquals": "COMPLETED",
                        "Next": "Notify",
                    },
                    {
                        "Variable": "$.execute.value.outcome",
                        "StringEquals": "STALE",
                        "Next": "ReturnToPlanning",
                    },
                    {
                        "Variable": "$.execute.value.outcome",
                        "StringEquals": "HALTED",
                        "Next": "Refused",
                    },
                ],
                "Default": "MarkFailedRolledBack",
            },
            "Notify": _task(
                function_arn,
                "Notify",
                "ScheduleGoodsReceiptCheck",
                {"documents.$": "$.execute.value.documents"},
            ),
            "ScheduleGoodsReceiptCheck": _task(
                function_arn,
                "ScheduleGoodsReceiptCheck",
                "MarkMonitoring",
                {"expectedArrival.$": "$.execute.value.expectedArrival"},
            ),
            "MarkMonitoring": _task(
                function_arn,
                "MarkMonitoring",
                "Done",
                {"documents.$": "$.execute.value.documents"},
            ),
            "Done": {"Type": "Succeed"},
            "ReturnToPlanning": _task(function_arn, "ReturnToPlanning", "ReturnedToPlanning"),
            "ReturnedToPlanning": {"Type": "Succeed"},
            "MarkFailedRolledBack": _task(
                function_arn,
                "MarkFailedRolledBack",
                "Escalated",
                {"reason.$": "$.execute.value.reason"},
            ),
            "Escalated": {
                "Type": "Fail",
                "Error": "EXECUTION_FAILED",
                "Cause": "Compensated and escalated (FR-EXE-07)",
            },
            "Refused": {
                "Type": "Fail",
                "Error": "EXECUTION_REFUSED",
                "Cause": "Kill switch on, or no persisted approval for this plan part",
            },
            "Rollback": rollback,
            "RolledBack": {"Type": "Succeed"},
            "RollbackFailed": {
                "Type": "Fail",
                "Error": "ROLLBACK_FAILED",
                "Cause": "Rollback refused or incomplete; manual reconciliation required",
            },
        },
    }
