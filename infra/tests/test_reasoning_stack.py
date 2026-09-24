"""WP-4 deployment boundaries (SRD 6.17/6.18, NFR-SEC-01, NFR-CMP-02)."""

import json
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import Stack, assertions

from infra.app import DataSettings, build_app
from services.tools.registry import TOOLS


@pytest.fixture(scope="module")
def deployment() -> tuple[dict[str, assertions.Template], Path]:
    app = build_app(DataSettings(env_name="dev", owner="synthetic-owner"))
    assembly = app.synth()
    templates = {
        name: assertions.Template.from_stack(Stack.of(app.node.find_child(f"aera-dev-{name}")))
        for name in ("reasoning", "edge")
    }
    return templates, Path(assembly.directory)


def statements(template: assertions.Template, role: str) -> list[dict[str, Any]]:
    return [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        if {"Ref": role} in policy["Properties"]["Roles"]
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def actions(statement: dict[str, Any]) -> list[str]:
    value = statement["Action"]
    return [value] if isinstance(value, str) else list(value)


def role_id(template: assertions.Template, prefix: str) -> str:
    return next(
        name for name in template.find_resources("AWS::IAM::Role") if name.startswith(prefix)
    )


def test_nfr_mnt_02_each_catalogue_tool_has_its_own_function_and_role(
    deployment: tuple[dict[str, assertions.Template], Path],
) -> None:
    template = deployment[0]["reasoning"]
    functions = template.find_resources("AWS::Lambda::Function")
    tools = [
        value["Properties"]
        for value in functions.values()
        if "AERA_TOOL_NAME" in value["Properties"]["Environment"]["Variables"]
    ]
    assert {value["Environment"]["Variables"]["AERA_TOOL_NAME"] for value in tools} == {
        spec.name for spec in TOOLS
    }
    assert len({json.dumps(value["Role"]) for value in tools}) == len(TOOLS)
    for value in tools:
        assert value["Handler"] == "services.tools.handler.lambda_handler"
        assert value["Runtime"] == "python3.12"
        assert value["Architectures"] == ["arm64"]
        assert "AERA_SAP_WRITES" not in value["Environment"]["Variables"]
    template.resource_count_is("AWS::Lambda::Function", len(TOOLS) + 1)
    template.resource_count_is("AWS::BedrockAgentCore::GatewayTarget", len(TOOLS))
    for target in template.find_resources("AWS::BedrockAgentCore::GatewayTarget").values():
        props = target["Properties"]
        schema = props["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]
        [tool] = schema
        spec = next(spec for spec in TOOLS if spec.name == tool["Name"])
        assert tool["InputSchema"]["Required"] == list(spec.required)
        assert set(tool["InputSchema"]["Properties"]) == set(spec.properties)
        assert props["CredentialProviderConfigurations"] == [
            {"CredentialProviderType": "GATEWAY_IAM_ROLE"}
        ]


def test_srd_6_18_container_lifecycle_and_asset_allowlist(
    deployment: tuple[dict[str, assertions.Template], Path],
) -> None:
    template, directory = deployment[0]["reasoning"], deployment[1]
    [resource] = template.find_resources("AWS::BedrockAgentCore::Runtime").values()
    props = resource["Properties"]
    assert set(props["AgentRuntimeArtifact"]) == {"ContainerConfiguration"}
    assert props["LifecycleConfiguration"] == {
        "IdleRuntimeSessionTimeout": 900,
        "MaxLifetime": 3600,
    }
    assets = json.loads((directory / "aera-dev-reasoning.assets.json").read_text())
    [image] = [
        asset
        for asset in assets["dockerImages"].values()
        if asset["source"]["dockerFile"] == "services/agent/Dockerfile"
    ]
    assert image["source"]["platform"] == "linux/arm64"
    root = directory / image["source"]["directory"]
    names = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    assert {
        "pyproject.toml",
        "uv.lock",
        "services/agent/Dockerfile",
        "services/agent/app.py",
    } <= names
    assert all(
        name in {"pyproject.toml", "uv.lock"} or name.startswith("services/") for name in names
    )
    assert not any(
        "/tests/" in name or "__pycache__" in name or name.endswith(".pyc") for name in names
    )
    assert not any(
        Path(name).name.startswith(".env")
        or Path(name).suffix in {".pem", ".key"}
        or "secrets" in Path(name).parts
        for name in names
    )


def test_nfr_cmp_02_only_direct_regional_models_and_cross_region_deny(
    deployment: tuple[dict[str, assertions.Template], Path],
) -> None:
    template = deployment[0]["reasoning"]
    policies = statements(template, role_id(template, "RuntimeRole"))
    inference = [item for item in policies if "bedrock:InvokeModel" in actions(item)]
    assert len(inference) == 2
    allowed = next(item for item in inference if item["Effect"] == "Allow")
    resources = json.dumps(allowed["Resource"])
    assert "foundation-model/anthropic.claude-" in resources
    assert "foundation-model/amazon.nova-" in resources
    assert "us-east-1" in resources
    assert "inference-profile" not in resources
    denied = next(item for item in inference if item["Effect"] == "Deny")
    assert denied["Resource"] == "*"
    assert denied["Condition"] == {"StringNotEquals": {"aws:RequestedRegion": "us-east-1"}}


def test_nfr_sec_01_no_execution_or_sap_write_privileges(
    deployment: tuple[dict[str, assertions.Template], Path],
) -> None:
    template = deployment[0]["reasoning"]
    policies = template.find_resources("AWS::IAM::Policy")
    text = json.dumps(policies)
    for forbidden in (
        "SAP_WRITE_BASE",
        "AERA_SAP_WRITES",
        "states:",
        "ses:",
        "sns:",
        "bedrock-agentcore:Create",
        "bedrock:Create",
    ):
        assert forbidden not in text
    for policy in policies.values():
        for item in policy["Properties"]["PolicyDocument"]["Statement"]:
            if item["Effect"] == "Allow":
                assert all(not action.endswith(":*") and action != "*" for action in actions(item))
    runtime = statements(template, role_id(template, "RuntimeRole"))
    assert not any("bedrock-agentcore:InvokeAgentRuntime" in actions(item) for item in runtime)
    gateway = statements(template, role_id(template, "GatewayRole"))
    assert all(action in {"lambda:InvokeFunction"} for item in gateway for action in actions(item))


def test_nfr_sec_01_parameter_scopes_and_empty_starter_allowlist(
    deployment: tuple[dict[str, assertions.Template], Path],
) -> None:
    template = deployment[0]["reasoning"]
    for function in template.find_resources("AWS::Lambda::Function").values():
        props = function["Properties"]
        role = props["Role"]["Fn::GetAtt"][0]
        params = [
            item for item in statements(template, role) if "ssm:GetParameter" in actions(item)
        ]
        if props["FunctionName"].endswith("run-starter"):
            assert not params
        else:
            assert len(params) == 1
            assert "SAP_READ_BASE" in json.dumps(params)
            assert "parameter/aera/dev/*" not in json.dumps(params)


def test_srd_6_17_api_only_emits_and_starter_invokes_runtime(
    deployment: tuple[dict[str, assertions.Template], Path],
) -> None:
    templates = deployment[0]
    assert "bedrock-agentcore:InvokeAgentRuntime" not in json.dumps(
        templates["edge"].find_resources("AWS::IAM::Policy")
    )
    template = templates["reasoning"]
    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "EventPattern": {"detail-type": ["CaseReadyForRun"], "source": [{"prefix": "aera."}]},
            "Targets": assertions.Match.array_with(
                [
                    assertions.Match.object_like(
                        {
                            "RetryPolicy": {"MaximumRetryAttempts": 4},
                            "DeadLetterConfig": assertions.Match.any_value(),
                        }
                    )
                ]
            ),
        },
    )
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {
            "SqsManagedSseEnabled": True,
            "MessageRetentionPeriod": 1209600,
        },
    )
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Gateway",
        {
            "AuthorizerType": "AWS_IAM",
            "ProtocolType": "MCP",
        },
    )
