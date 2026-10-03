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


def test_fr_lrn_01_reliability_refresh_is_scheduled_daily(
    templates: dict[str, assertions.Template],
) -> None:
    control = templates["control"]
    rules = control.find_resources("AWS::Events::Rule")
    [rule] = [
        r for r in rules.values() if r["Properties"]["Name"] == "aera-dev-reliability-refresh"
    ]
    assert rule["Properties"]["ScheduleExpression"] == "rate(1 day)"
    functions = control.find_resources("AWS::Lambda::Function")
    assert any(
        f["Properties"]["FunctionName"] == "aera-dev-reliability" for f in functions.values()
    )


def test_fr_ver_04_reasoning_policy_guardrail_and_verifier_wiring(
    templates: dict[str, assertions.Template],
) -> None:
    control = templates["control"]
    policies = control.find_resources("AWS::Bedrock::AutomatedReasoningPolicy")
    [policy] = policies.values()
    rules = policy["Properties"]["PolicyDefinition"]["Rules"]
    assert {rule["Id"] for rule in rules} == {"BR05AUTO0001", "BR07DONOR001", "BR12RAR00001"}
    assert {rule["Id"]: rule["Expression"] for rule in rules} == {
        "BR05AUTO0001": (
            "(=> (= tier 1) (and (<= total_cost_cents auto_limit_cents) "
            "(>= confidence confidence_min) all_reversible))"
        ),
        "BR07DONOR001": "(=> (or (= tier 1) (= tier 2)) donor_protection_ok)",
        "BR12RAR00001": "(=> (or (= tier 1) (= tier 2)) (< total_cost_cents rar_protected_cents))",
    }
    guardrails = control.find_resources("AWS::Bedrock::Guardrail")
    assert any("AutomatedReasoningPolicyConfig" in row["Properties"] for row in guardrails.values())
    params = control.find_resources("AWS::SSM::Parameter")
    assert {row["Properties"]["Name"] for row in params.values()} >= {
        "/aera/dev/REASONING_POLICY_ARN",
        "/aera/dev/REASONING_GUARDRAIL_ID",
        "/aera/dev/REASONING_GUARDRAIL_VERSION",
    }


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
        if "EventPattern" in r["Properties"]
    }
    assert rules["aera-dev-notifier"] == ["NotificationRequested"]
    assert rules["aera-dev-supplierdialogue"] == ["SupplierInfoRequested"]
    assert rules["aera-dev-monitor"] == ["GoodsReceiptDue"]
    sweep = templates["control"].find_resources(
        "AWS::Events::Rule", {"Properties": {"Name": "aera-dev-dialogue-sweep"}}
    )
    assert len(sweep) == 1


def test_srd_6_6_verifier_runs_on_plan_proposed_and_cannot_write_to_sap(
    templates: dict[str, assertions.Template],
) -> None:
    control = templates["control"]
    rules = {
        r["Properties"]["Name"]: r["Properties"]["EventPattern"]["detail-type"]
        for r in control.find_resources("AWS::Events::Rule").values()
        if "EventPattern" in r["Properties"]
    }
    assert rules["aera-dev-verifier"] == ["PlanProposed"]
    env = lambda_env(control)["aera-dev-verifier"]
    assert "AERA_SAP_WRITES" not in env and "AERA_APPROVAL_TIMER_ARN" in env
    policies = [
        json.dumps(p)
        for p in control.find_resources("AWS::IAM::Policy").values()
        if "verifier" in json.dumps(p["Properties"]["Roles"]).lower()
    ]
    assert policies and not any("SAP_WRITE_BASE" in p or "ses:Send" in p for p in policies)
    assert any("bedrock:ApplyGuardrail" in p for p in policies)


def test_fr_ver_04_reasoning_policy_uses_the_service_rule_language(
    templates: dict[str, assertions.Template],
) -> None:
    """The service takes SMT-LIB prefix expressions and upper-case built-in types only."""
    [policy] = (
        templates["control"].find_resources("AWS::Bedrock::AutomatedReasoningPolicy").values()
    )
    definition = policy["Properties"]["PolicyDefinition"]
    variables = {row["Name"]: row["Type"] for row in definition["Variables"]}

    assert set(variables.values()) <= {"INT", "BOOL"}
    operators = {"=>", "=", "and", "or", "<", "<=", ">", ">="}
    for rule in definition["Rules"]:
        expression = rule["Expression"]
        assert expression.startswith("(=> ") and expression.count("(") == expression.count(")")
        symbols = expression.replace("(", " ").replace(")", " ").split()
        unknown = [s for s in symbols if s not in operators | set(variables) and not s.isdigit()]
        assert unknown == []


def test_adr_0026_only_the_reasoning_guardrail_uses_the_us_guardrail_profile(
    templates: dict[str, assertions.Template],
) -> None:
    """Automated Reasoning checks need a cross-Region guardrail profile; nothing else gets one."""
    control = templates["control"]
    guardrails = control.find_resources("AWS::Bedrock::Guardrail").values()
    [reasoning] = [g for g in guardrails if "AutomatedReasoningPolicyConfig" in g["Properties"]]
    profile = json.dumps(reasoning["Properties"]["CrossRegionConfig"]["GuardrailProfileArn"])
    assert ":guardrail-profile/us.guardrail.v1:0" in profile and "us-east-1" in profile
    assert "global." not in profile

    statements = [
        statement
        for policy in control.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if "bedrock:ApplyGuardrail" in json.dumps(statement["Action"])
    ]
    assert any(
        ":guardrail-profile/us.guardrail.v1:0" in json.dumps(s["Resource"]) for s in statements
    )
    # ADR-0030: the profile may route to another US Region; the same guardrail id is allowed
    # there and nothing broader.
    resources = json.dumps([s["Resource"] for s in statements])
    assert (
        '":bedrock:*:"' in resources
        and '":guardrail/", {"Fn::GetAtt": ["ReasoningGuardrail", "GuardrailId"]}' in resources
    )
    assert '":bedrock:*:", {"Ref": "AWS::AccountId"}, ":guardrail/*"' not in resources


def test_adr_0026_input_guardrail_stays_in_region() -> None:
    from infra.app import DataSettings, build_app

    app = build_app(DataSettings(env_name="dev", owner="synthetic-owner"))
    gate = assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-gate")))
    for guardrail in gate.find_resources("AWS::Bedrock::Guardrail").values():
        assert "CrossRegionConfig" not in guardrail["Properties"]
