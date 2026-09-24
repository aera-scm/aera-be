"""Control stack and execution workflow definition (SRD 6.8, FR-EXE-01, NFR-SEC-01)."""

import json
from typing import Any

import pytest
from aws_cdk import Stack, assertions

from infra.app import DataSettings, build_app
from infra.constructs.execution_workflow import definition

FN = "arn:aws:lambda:us-east-1:000000000000:function:aera-dev-execution"


@pytest.fixture(scope="module")
def templates() -> dict[str, assertions.Template]:
    app = build_app(DataSettings(env_name="dev", owner="aera-test-owner"))
    return {
        name: assertions.Template.from_stack(Stack.of(app.node.find_child(f"aera-dev-{name}")))
        for name in ("control", "reasoning", "gate", "edge")
    }


def walk(asl: dict[str, Any], choices: dict[str, str]) -> list[str]:
    """Follow the definition from StartAt, taking the given branch at each Choice."""
    states, name, path = asl["States"], asl["StartAt"], []
    while True:
        path.append(name)
        state = states[name]
        if state["Type"] in ("Succeed", "Fail") or state.get("End"):
            return path
        if state["Type"] == "Choice":
            name = choices.get(name, state["Default"])
        else:
            name = state["Next"]


def test_srd_6_8_happy_path_order() -> None:
    assert walk(
        definition(FN), {"KillSwitchOn": "Execute", "Outcome": "Notify", "Mode": "CheckKillSwitch"}
    ) == [
        "Mode",
        "CheckKillSwitch",
        "KillSwitchOn",
        "Execute",
        "Outcome",
        "Notify",
        "ScheduleGoodsReceiptCheck",
        "MarkMonitoring",
        "Done",
    ]


def test_srd_6_8_failure_kill_switch_stale_and_rollback_paths() -> None:
    asl = definition(FN)
    assert walk(asl, {"KillSwitchOn": "Refused"})[-1] == "Refused"
    assert walk(asl, {"KillSwitchOn": "Execute", "Outcome": "ReturnToPlanning"})[-2:] == [
        "ReturnToPlanning",
        "ReturnedToPlanning",
    ]
    assert walk(asl, {"KillSwitchOn": "Execute"})[-2:] == ["MarkFailedRolledBack", "Escalated"]
    assert walk(asl, {"Mode": "Rollback"}) == ["Mode", "Rollback", "RolledBack"]
    outcome = {c["StringEquals"]: c["Next"] for c in asl["States"]["Outcome"]["Choices"]}
    assert outcome == {"COMPLETED": "Notify", "STALE": "ReturnToPlanning", "HALTED": "Refused"}


def test_every_task_is_an_idempotent_step_with_bounded_retries() -> None:
    for name, state in definition(FN)["States"].items():
        if state["Type"] != "Task":
            continue
        assert state["Parameters"]["Payload"]["step"] == name
        [retry] = state["Retry"]
        assert retry["MaxAttempts"] == 3 and retry["BackoffRate"] == 2.0


def test_plan_approved_starts_the_standard_state_machine(
    templates: dict[str, assertions.Template],
) -> None:
    control = templates["control"]
    control.has_resource_properties(
        "AWS::StepFunctions::StateMachine",
        {"StateMachineName": "aera-dev-execution", "StateMachineType": "STANDARD"},
    )
    rules = control.find_resources("AWS::Events::Rule")
    [rule] = [r for r in rules.values() if r["Properties"]["Name"] == "aera-dev-execution"]
    assert rule["Properties"]["EventPattern"]["detail-type"] == ["PlanApproved"]


def lambda_env(template: assertions.Template) -> dict[str, dict[str, Any]]:
    return {
        f["Properties"]["FunctionName"]: f["Properties"].get("Environment", {}).get("Variables", {})
        for f in template.find_resources("AWS::Lambda::Function").values()
    }


def test_fr_exe_01_only_the_execution_task_can_write_to_sap(
    templates: dict[str, assertions.Template],
) -> None:
    writers = [
        name
        for template in templates.values()
        for name, env in lambda_env(template).items()
        if env.get("AERA_SAP_WRITES") == "1"
    ]
    assert writers == ["aera-dev-execution"]
    for name, template in templates.items():
        policies = json.dumps(template.find_resources("AWS::IAM::Policy"))
        if name != "control":
            assert "SAP_WRITE_BASE" not in policies
    reasoning = json.dumps(templates["reasoning"].to_json())
    assert "states:StartExecution" not in reasoning
    assert "ses:Send" not in reasoning


def test_outbox_relay_reads_only_outbox_inserts(templates: dict[str, assertions.Template]) -> None:
    [mapping] = templates["control"].find_resources("AWS::Lambda::EventSourceMapping").values()
    [pattern] = mapping["Properties"]["FilterCriteria"]["Filters"]
    assert json.loads(pattern["Pattern"]) == {
        "eventName": ["INSERT"],
        "dynamodb": {"Keys": {"SK": {"S": [{"prefix": "OUTBOX#"}]}}},
    }


def test_fr_com_02_only_the_notifier_can_send_mail(
    templates: dict[str, assertions.Template],
) -> None:
    senders = [
        name
        for name, template in templates.items()
        for policy in template.find_resources("AWS::IAM::Policy").values()
        if "ses:SendEmail" in json.dumps(policy)
    ]
    assert senders == ["control"]
    [policy] = [
        p
        for p in templates["control"].find_resources("AWS::IAM::Policy").values()
        if "ses:SendEmail" in json.dumps(p)
    ]
    assert "notifier" in json.dumps(policy["Properties"]["Roles"]).lower()


def test_notifier_and_monitor_listen_on_the_bus(
    templates: dict[str, assertions.Template],
) -> None:
    rules = {
        r["Properties"]["Name"]: r["Properties"]["EventPattern"]["detail-type"]
        for r in templates["control"].find_resources("AWS::Events::Rule").values()
    }
    assert rules["aera-dev-notifier"] == ["NotificationRequested"]
    assert rules["aera-dev-monitor"] == ["GoodsReceiptDue"]
