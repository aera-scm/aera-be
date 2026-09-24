"""FR-INT / BR-21: only the closed service API crosses the runtime boundary."""

import json
from pathlib import Path

from aws_cdk import Stack, assertions

from infra.app import DataSettings, build_app


def test_fr_int_agentcore_runtimes_require_scoped_oauth_client() -> None:
    app = build_app(DataSettings(env_name="dev", owner="synthetic-owner"))
    assembly = app.synth()
    interop = assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-interop")))
    identity = assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-identity")))
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
            "AERA_INTEROP_API_FUNCTION",
        }
        assert "AERA_SAP_WRITES" not in variables
    clients = identity.find_resources("AWS::Cognito::UserPoolClient")
    [machine] = [
        value["Properties"]
        for value in clients.values()
        if value["Properties"]["AllowedOAuthFlows"] == ["client_credentials"]
    ]
    assert machine["GenerateSecret"] is True
    assert machine["AllowedOAuthScopes"] == ["aera-dev-interop/invoke"]
    policies = json.dumps(interop.find_resources("AWS::IAM::Policy"))
    assert "lambda:InvokeFunction" in policies
    for forbidden in ("states:", "ses:", "dynamodb:", "secretsmanager:", "s3:"):
        assert forbidden not in policies
    assets = json.loads((Path(assembly.directory) / "aera-dev-interop.assets.json").read_text())
    images = [
        asset
        for asset in assets["dockerImages"].values()
        if asset["source"]["dockerFile"] == "services/interop/Dockerfile"
    ]
    assert len(images) == 1 and images[0]["source"]["platform"] == "linux/arm64"
