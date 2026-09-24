"""M1 gate and edge stacks (SRD 6.15-6.17, 6.10, FR-ING-01..05, NFR-SEC-04, IR-06..08)."""

import json
from typing import Any

import pytest
from aws_cdk import Stack, assertions

from infra.app import DataSettings, build_app

SETTINGS = DataSettings(env_name="dev", owner="aera-test-owner")


@pytest.fixture(scope="module")
def stacks() -> dict[str, assertions.Template]:
    app = build_app(SETTINGS)
    return {
        name: assertions.Template.from_stack(Stack.of(app.node.find_child(f"aera-dev-{name}")))
        for name in ("gate", "edge")
    }


def functions(template: assertions.Template) -> dict[str, dict[str, Any]]:
    return {
        f["Properties"]["FunctionName"]: f["Properties"]
        for f in template.find_resources("AWS::Lambda::Function").values()
    }


def policy_text(template: assertions.Template) -> str:
    return json.dumps(template.find_resources("AWS::IAM::Policy"))


def test_srd_6_15_components_are_named_and_built_alike(
    stacks: dict[str, assertions.Template],
) -> None:
    gate, edge = functions(stacks["gate"]), functions(stacks["edge"])

    assert set(gate) == {
        "aera-dev-gatekeeper",
        "aera-dev-extraction",
        "aera-dev-case-service",
        "aera-dev-mrp-poller",
    }
    assert set(edge) == {
        "aera-dev-api",
        "aera-dev-webhooks",
        "aera-dev-ses-inbound",
        "aera-dev-realtime",
    }
    for name, props in {**gate, **edge}.items():
        component = name.removeprefix("aera-dev-")
        assert props["Runtime"] == "python3.12"
        assert props["Architectures"] == ["arm64"]
        assert props["TracingConfig"] == {"Mode": "Active"}
        assert props["Handler"] == f"services.{component.replace('-', '_')}.handler.lambda_handler"
        assert props["Environment"]["Variables"]["AERA_ENV"] == "dev"


def test_case_service_runs_one_at_a_time(stacks: dict[str, assertions.Template]) -> None:
    assert functions(stacks["gate"])["aera-dev-case-service"]["ReservedConcurrentExecutions"] == 1


def test_srd_6_17_pipeline_rules_on_the_env_bus(stacks: dict[str, assertions.Template]) -> None:
    rules = {
        r["Properties"]["Name"]: r["Properties"]
        for r in stacks["gate"].find_resources("AWS::Events::Rule").values()
    }

    assert rules["aera-dev-gatekeeper"]["EventPattern"]["detail-type"] == ["SignalReceived"]
    assert rules["aera-dev-extraction"]["EventPattern"]["detail-type"] == ["SignalAccepted"]
    assert rules["aera-dev-caseservice"]["EventPattern"]["detail-type"] == [
        "SignalExtracted",
        "MrpExceptionsPolled",
    ]
    for rule in rules.values():
        assert rule["EventPattern"]["source"] == [{"prefix": "aera."}]
        [target] = rule["Targets"]
        assert target["RetryPolicy"]["MaximumRetryAttempts"] == 8
        assert "DeadLetterConfig" in target


def test_fr_ing_01_mrp_poller_runs_every_15_minutes(stacks: dict[str, assertions.Template]) -> None:
    stacks["gate"].has_resource_properties(
        "AWS::Scheduler::Schedule",
        {"Name": "aera-dev-mrp-poller", "ScheduleExpression": "rate(15 minutes)"},
    )


def test_fr_ing_05_guardrail_blocks_prompt_attacks_at_high_strength(
    stacks: dict[str, assertions.Template],
) -> None:
    gate = stacks["gate"]
    gate.has_resource_properties(
        "AWS::Bedrock::Guardrail",
        {
            "ContentPolicyConfig": {
                "FiltersConfig": [
                    {"Type": "PROMPT_ATTACK", "InputStrength": "HIGH", "OutputStrength": "NONE"}
                ]
            }
        },
    )
    names = {p["Properties"]["Name"] for p in gate.find_resources("AWS::SSM::Parameter").values()}
    assert names == {"/aera/dev/GUARDRAIL_ID", "/aera/dev/GUARDRAIL_VERSION"}


def test_least_privilege_for_ocr_and_guardrail(stacks: dict[str, assertions.Template]) -> None:
    policies = stacks["gate"].find_resources("AWS::IAM::Policy")
    by_role = {
        json.dumps(p["Properties"]["Roles"]): json.dumps(p["Properties"]["PolicyDocument"])
        for p in policies.values()
    }
    textract = [role for role, doc in by_role.items() if "textract:AnalyzeDocument" in doc]
    guardrail = [role for role, doc in by_role.items() if "bedrock:ApplyGuardrail" in doc]
    assert len(textract) == 1 and "extraction" in textract[0]
    assert len(guardrail) == 2
    assert all("gatekeeper" in r or "extraction" in r for r in guardrail)
    assert "bedrock:InvokeModel" not in policy_text(stacks["gate"])


def methods(template: assertions.Template) -> dict[tuple[str, str], str]:
    resources = template.find_resources("AWS::ApiGateway::Resource")
    paths: dict[str, str] = {}

    def path(logical: str) -> str:
        if logical not in paths:
            props = resources[logical]["Properties"]
            parent = props["ParentId"]
            prefix = path(parent["Ref"]) if "Ref" in parent else ""
            paths[logical] = f"{prefix}/{props['PathPart']}"
        return paths[logical]

    found = {}
    for method in template.find_resources("AWS::ApiGateway::Method").values():
        props = method["Properties"]
        if props["HttpMethod"] == "OPTIONS":
            continue
        resource = props["ResourceId"]
        where = path(resource["Ref"]) if "Ref" in resource else "/"
        found[(props["HttpMethod"], where)] = props["AuthorizationType"]
    return found


def test_nfr_sec_04_console_routes_need_cognito_webhooks_need_signatures(
    stacks: dict[str, assertions.Template],
) -> None:
    routes = methods(stacks["edge"])

    assert routes == {
        ("GET", "/cases"): "COGNITO_USER_POOLS",
        ("GET", "/cases/{id}"): "COGNITO_USER_POOLS",
        ("GET", "/cases/{id}/trace"): "COGNITO_USER_POOLS",
        ("POST", "/cases/{id}/runs"): "COGNITO_USER_POOLS",
        ("POST", "/cases/{id}/rollback"): "COGNITO_USER_POOLS",
        ("POST", "/cases/{id}/chat"): "COGNITO_USER_POOLS",
        ("POST", "/cases/{id}/approval"): "COGNITO_USER_POOLS",
        ("GET", "/cases/{id}/projection"): "COGNITO_USER_POOLS",
        ("POST", "/cases/{id}/whatif"): "COGNITO_USER_POOLS",
        ("PUT", "/admin/config/{key}"): "COGNITO_USER_POOLS",
        ("POST", "/admin/killswitch"): "COGNITO_USER_POOLS",
        ("POST", "/admin/reset"): "COGNITO_USER_POOLS",
        ("POST", "/cases/{id}/fields/{fieldId}/confirm"): "COGNITO_USER_POOLS",
        ("GET", "/signals"): "COGNITO_USER_POOLS",
        ("POST", "/signals"): "COGNITO_USER_POOLS",
        ("GET", "/metrics"): "COGNITO_USER_POOLS",
        ("POST", "/realtime/ticket"): "COGNITO_USER_POOLS",
        ("GET", "/webhooks/whatsapp"): "NONE",
        ("POST", "/webhooks/whatsapp"): "NONE",
        ("POST", "/webhooks/carrier"): "NONE",
    }


def test_srd_6_17_websocket_routes_and_live_push(stacks: dict[str, assertions.Template]) -> None:
    edge = stacks["edge"]
    keys = {
        r["Properties"]["RouteKey"]
        for r in edge.find_resources("AWS::ApiGatewayV2::Route").values()
    }
    assert keys == {"$connect", "$disconnect", "subscribe"}
    edge.resource_count_is("AWS::Lambda::EventSourceMapping", 1)
    rules = {
        r["Properties"]["Name"]: r["Properties"]
        for r in edge.find_resources("AWS::Events::Rule").values()
    }
    assert "CaseUpdated" in rules["aera-dev-realtime"]["EventPattern"]["detail-type"]
    assert "execute-api:ManageConnections" in policy_text(edge)


def test_ir_06_supplier_mail_is_stored_under_ses_and_triggers_intake(
    stacks: dict[str, assertions.Template],
) -> None:
    edge = stacks["edge"]
    rule = next(iter(edge.find_resources("AWS::SES::ReceiptRule").values()))["Properties"]["Rule"]
    assert rule["ScanEnabled"] is True and rule["TlsPolicy"] == "Require"
    assert rule["Actions"][0]["S3Action"]["ObjectKeyPrefix"] == "ses/"
    rules = {
        r["Properties"]["Name"]: r["Properties"]
        for r in edge.find_resources("AWS::Events::Rule").values()
    }
    pattern = rules["aera-dev-ses-inbound"]["EventPattern"]
    assert pattern["source"] == ["aws.s3"] and pattern["detail-type"] == ["Object Created"]
    assert pattern["detail"]["object"] == {"key": [{"prefix": "ses/"}]}


def test_endpoints_are_published_for_tools_and_console(
    stacks: dict[str, assertions.Template],
) -> None:
    names = {
        p["Properties"]["Name"]
        for p in stacks["edge"].find_resources("AWS::SSM::Parameter").values()
    }
    assert names == {"/aera/dev/API_URL", "/aera/dev/WS_URL"}


def test_no_secret_values_in_gate_or_edge(stacks: dict[str, assertions.Template]) -> None:
    for template in stacks.values():
        text = json.dumps(template.to_json())
        assert "SecretString" not in text
        assert "synthetic" not in text.lower()
