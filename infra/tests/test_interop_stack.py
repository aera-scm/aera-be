"""FR-INT / BR-21: only the closed service API crosses the runtime boundary."""

import json
from pathlib import Path

import pytest
from aws_cdk import Stack, assertions

from infra.app import DataSettings, build_app


def test_fr_int_agentcore_runtimes_require_scoped_oauth_client() -> None:
    app = build_app(DataSettings(env_name="dev", owner="synthetic-owner"))
    assembly = app.synth()
    interop = assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-interop")))
    identity = assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-identity")))
    edge = assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-edge")))
    runtimes = interop.find_resources("AWS::BedrockAgentCore::Runtime")
    assert len(runtimes) == 2
    assert {row["Properties"]["ProtocolConfiguration"] for row in runtimes.values()} == {
        "A2A",
        "MCP",
    }
    for row in runtimes.values():
        props = row["Properties"]
        jwt = props["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
        assert jwt["AllowedScopes"] == ["aera-dev-interop/invoke"]
        assert len(jwt["AllowedClients"]) == 1
        assert "cognito-idp.us-east-1.amazonaws.com" in json.dumps(jwt["DiscoveryUrl"])
        assert props["RequestHeaderConfiguration"] == {"RequestHeaderAllowlist": ["Authorization"]}
        variables = props["EnvironmentVariables"]
        assert set(variables) == {
            "AERA_ENV",
            "AERA_INTEROP_PROTOCOL",
            "AERA_INTEROP_CLIENT_ID",
            "AERA_INTEROP_API_URL",
            "AERA_INTEROP_PROVIDER",
            "AERA_INTEROP_WORKLOAD",
            "AERA_INTEROP_SCOPE",
        }
        assert "/interop" in json.dumps(variables["AERA_INTEROP_API_URL"])
        assert "AERA_SAP_WRITES" not in variables
    clients = identity.find_resources("AWS::Cognito::UserPoolClient")
    machine = [
        value["Properties"]
        for value in clients.values()
        if value["Properties"]["AllowedOAuthFlows"] == ["client_credentials"]
    ]
    assert len(machine) == 2
    assert all(row["GenerateSecret"] is True for row in machine)
    assert all(row["AllowedOAuthScopes"] == ["aera-dev-interop/invoke"] for row in machine)
    identity.resource_count_is("AWS::BedrockAgentCore::OAuth2CredentialProvider", 1)
    provider = next(
        iter(identity.find_resources("AWS::BedrockAgentCore::OAuth2CredentialProvider").values())
    )
    secret = provider["Properties"]["Oauth2ProviderConfigInput"]["CustomOauth2ProviderConfig"][
        "ClientSecret"
    ]
    assert "Fn::GetAtt" in secret
    interop.resource_count_is("AWS::BedrockAgentCore::WorkloadIdentity", 2)
    scoped = [
        row["Properties"]
        for row in edge.find_resources("AWS::ApiGateway::Method").values()
        if row["Properties"].get("AuthorizationScopes") == ["aera-dev-interop/invoke"]
    ]
    assert len(scoped) == 1 and scoped[0]["HttpMethod"] == "POST"
    policies = json.dumps(interop.find_resources("AWS::IAM::Policy"))
    assert "lambda:InvokeFunction" not in policies
    assert "bedrock-agentcore:GetResourceOauth2Token" in policies
    for forbidden in ("states:", "ses:", "dynamodb:", "s3:"):
        assert forbidden not in policies
    assets = json.loads((Path(assembly.directory) / "aera-dev-interop.assets.json").read_text())
    images = [
        asset
        for asset in assets["dockerImages"].values()
        if asset["source"]["dockerFile"] == "services/interop/Dockerfile"
    ]
    assert len(images) == 1 and images[0]["source"]["platform"] == "linux/arm64"


def test_fr_lab_02_channel_replay_requires_recorded_media_and_scoped_secrets() -> None:
    with pytest.raises(ValueError, match="recorded-media"):
        build_app(
            DataSettings(env_name="dev", owner="synthetic-owner", lab_delivery="channel-replay")
        )
    app = build_app(
        DataSettings(
            env_name="dev",
            owner="synthetic-owner",
            lab_delivery="channel-replay",
            whatsapp_media="replay",
        )
    )
    template = assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-edge")))
    api = next(
        row["Properties"]
        for row in template.find_resources("AWS::Lambda::Function").values()
        if row["Properties"]["FunctionName"] == "aera-dev-api"
    )
    variables = api["Environment"]["Variables"]
    assert variables["AERA_LAB_DELIVERY"] == "channel-replay"
    assert variables["AERA_WHATSAPP_MEDIA"] == "replay"
    policies = json.dumps(template.find_resources("AWS::IAM::Policy"))
    assert "channels/whatsapp" in policies and "channels/carrier-webhook" in policies
