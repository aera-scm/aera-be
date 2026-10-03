"""SAP Mirror on ECS Fargate, the SRD 6.15 fallback (IR-02, IR-03, NFR-SEC-03/04/05)."""

import json
from typing import Any

import pytest
from aws_cdk import Stack, assertions

from infra.app import DataSettings, build_app, data_settings_from_environment

BASE = {"AERA_ENV": "dev", "AERA_OWNER_TAG": "aera-test-owner"}


@pytest.fixture(scope="module")
def mirror() -> assertions.Template:
    app = build_app(DataSettings(env_name="dev", owner="aera-test-owner", mirror_hosting="ecs"))
    return assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-mirror")))


def only(template: assertions.Template, kind: str) -> dict[str, Any]:
    [resource] = template.find_resources(kind).values()
    return dict(resource["Properties"])


def test_srd_6_16_mirror_stack_exists_only_with_the_fallback() -> None:
    default = build_app(DataSettings(env_name="dev", owner="aera-test-owner"))
    fallback = build_app(
        DataSettings(env_name="dev", owner="aera-test-owner", mirror_hosting="ecs")
    )

    assert default.node.try_find_child("aera-dev-mirror") is None
    assert len(default.synth().stacks) == 9
    assembly = fallback.synth()
    assert len(assembly.stacks) == 10
    stack = Stack.of(fallback.node.find_child("aera-dev-mirror"))
    assert {dependency.stack_name for dependency in stack.dependencies} == {
        "aera-dev-data",
        "aera-dev-identity",
    }
    observability = Stack.of(fallback.node.find_child("aera-dev-observability"))
    assert "aera-dev-mirror" in {dependency.stack_name for dependency in observability.dependencies}


@pytest.mark.parametrize(("value", "expected"), [(None, None), ("", None), ("ecs", "ecs")])
def test_mirror_hosting_is_read_from_the_environment(
    value: str | None, expected: str | None
) -> None:
    environ = dict(BASE) if value is None else {**BASE, "AERA_MIRROR_HOSTING": value}

    assert data_settings_from_environment(environ).mirror_hosting == expected


@pytest.mark.parametrize("value", ["btp", "ECS", "lambda", "true"])
def test_mirror_hosting_refuses_other_values(value: str) -> None:
    with pytest.raises(ValueError, match="AERA_MIRROR_HOSTING"):
        data_settings_from_environment({**BASE, "AERA_MIRROR_HOSTING": value})


def test_ir_02_one_small_arm64_task_runs_the_mirror(mirror: assertions.Template) -> None:
    task = only(mirror, "AWS::ECS::TaskDefinition")
    assert task["Cpu"] == "256" and task["Memory"] == "512"
    assert task["RequiresCompatibilities"] == ["FARGATE"]
    assert task["RuntimePlatform"] == {
        "CpuArchitecture": "ARM64",
        "OperatingSystemFamily": "LINUX",
    }
    [container] = task["ContainerDefinitions"]
    assert container["PortMappings"] == [{"ContainerPort": 4004, "Protocol": "tcp"}]
    assert "HealthCheck" in container

    service = only(mirror, "AWS::ECS::Service")
    assert service["DesiredCount"] == 1
    assert service["LaunchType"] == "FARGATE"
    # In-memory state: never two tasks at once, even during a deployment.
    assert service["DeploymentConfiguration"]["MaximumPercent"] == 100
    assert service["DeploymentConfiguration"]["MinimumHealthyPercent"] == 0


def test_nfr_sec_04_mirror_trusts_only_its_own_cognito_client(
    mirror: assertions.Template,
) -> None:
    client = only(mirror, "AWS::Cognito::UserPoolClient")
    assert client["GenerateSecret"] is True
    assert client["AllowedOAuthFlows"] == ["client_credentials"]
    assert client["AllowedOAuthScopes"] == ["aera-dev-mirror/access", "aera-dev-mirror/admin"]
    server = only(mirror, "AWS::Cognito::UserPoolResourceServer")
    assert server["Identifier"] == "aera-dev-mirror"
    assert {scope["ScopeName"] for scope in server["Scopes"]} == {"access", "admin"}

    [container] = only(mirror, "AWS::ECS::TaskDefinition")["ContainerDefinitions"]
    environment = {row["Name"]: row["Value"] for row in container["Environment"]}
    assert environment["CDS_ENV"] == "aws"
    assert environment["MIRROR_OAUTH_SCOPE"] == "aera-dev-mirror/access"
    assert environment["MIRROR_OAUTH_ADMIN_SCOPE"] == "aera-dev-mirror/admin"
    issuer = json.dumps(environment["MIRROR_OAUTH_ISSUER"])
    assert "https://cognito-idp.us-east-1.amazonaws.com/" in issuer
    assert "Ref" in json.dumps(environment["MIRROR_OAUTH_CLIENT_ID"])
    # The client secret is never a container, template or output value.
    text = json.dumps(mirror.to_json())
    assert "ClientSecret" not in text


def test_nfr_sec_05_task_is_reachable_only_through_the_https_api(
    mirror: assertions.Template,
) -> None:
    mirror.resource_count_is("AWS::EC2::NatGateway", 0)
    mirror.resource_count_is("AWS::ElasticLoadBalancingV2::LoadBalancer", 0)
    groups = mirror.find_resources("AWS::EC2::SecurityGroup")
    for group in groups.values():
        assert not group["Properties"].get("SecurityGroupIngress")
    [ingress] = mirror.find_resources("AWS::EC2::SecurityGroupIngress").values()
    rule = ingress["Properties"]
    assert rule["FromPort"] == rule["ToPort"] == 4004
    assert "SourceSecurityGroupId" in rule and "CidrIp" not in rule

    api = only(mirror, "AWS::ApiGatewayV2::Api")
    assert api["ProtocolType"] == "HTTP"
    integration = only(mirror, "AWS::ApiGatewayV2::Integration")
    assert integration["ConnectionType"] == "VPC_LINK"
    assert integration["IntegrationType"] == "HTTP_PROXY"
    stage = only(mirror, "AWS::ApiGatewayV2::Stage")
    assert stage["DefaultRouteSettings"]["ThrottlingRateLimit"] > 0


def test_nfr_sec_03_registration_can_touch_only_the_mirror_client_secret_and_urls(
    mirror: assertions.Template,
) -> None:
    registration = only(mirror, "AWS::CloudFormation::CustomResource")
    assert registration["SecretId"] == "/aera/dev/sap/mirror-oauth-client"
    assert registration["Parameters"] == ["/aera/dev/SAP_READ_BASE", "/aera/dev/SAP_WRITE_BASE"]
    assert "ClientSecret" not in registration

    functions = mirror.find_resources("AWS::Lambda::Function")
    [registrar_id] = [
        name
        for name, function in functions.items()
        if function["Properties"]["Handler"] == "index.handler"
    ]
    role = functions[registrar_id]["Properties"]["Role"]["Fn::GetAtt"][0]
    statements = [
        statement
        for policy in mirror.find_resources("AWS::IAM::Policy").values()
        if {"Ref": role} in policy["Properties"]["Roles"]
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    by_action = {json.dumps(s["Action"]): json.dumps(s["Resource"]) for s in statements}
    assert '"cognito-idp:DescribeUserPoolClient"' in by_action
    secret = by_action['"secretsmanager:PutSecretValue"']
    assert "secret:/aera/dev/sap/mirror-oauth-client-" in secret and '"*"' not in secret
    parameters = by_action['"ssm:PutParameter"']
    assert "parameter/aera/dev/SAP_READ_BASE" in parameters
    assert "parameter/aera/dev/SAP_WRITE_BASE" in parameters
    assert all(s["Effect"] == "Allow" and s["Resource"] != "*" for s in statements)
