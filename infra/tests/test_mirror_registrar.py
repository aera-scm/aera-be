"""The custom resource that registers the ECS-hosted Mirror with AERA (SRD 6.16, IR-02,
NFR-SEC-03). Runs against moto; a stubbed account is not deployment evidence."""

import importlib.util
import json
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

SOURCE = Path(__file__).resolve().parents[1] / "assets/mirror_registrar/index.py"
SECRET_ID = "/aera/dev/sap/mirror-oauth-client"  # pragma: allowlist secret (a name)
PARAMETERS = ["/aera/dev/SAP_READ_BASE", "/aera/dev/SAP_WRITE_BASE"]
MIRROR_URL = "https://abc123.execute-api.us-east-1.amazonaws.com"
TOKEN_URL = "https://aera-dev-1a2b3c4d.auth.us-east-1.amazoncognito.com/oauth2/token"
POOL = "us-east-1_SYNTHETIC"
CLIENT = "syntheticmirrorclient0000"
CLIENT_SECRET = "synthetic-client-secret-value"  # pragma: allowlist secret


@pytest.fixture
def registrar(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # pragma: allowlist secret
    with mock_aws():
        spec = importlib.util.spec_from_file_location("mirror_registrar", SOURCE)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # The user pool is a botocore stub checked against the service model; Secrets
        # Manager and SSM are moto.
        cognito = boto3.client("cognito-idp")
        stub = Stubber(cognito)
        stub.add_response(
            "describe_user_pool_client",
            {"UserPoolClient": {"ClientId": CLIENT, "ClientSecret": CLIENT_SECRET}},
            {"UserPoolId": POOL, "ClientId": CLIENT},
        )
        stub.activate()
        real = boto3.client
        monkeypatch.setattr(
            module.boto3,
            "client",
            lambda name, *args, **kwargs: (
                cognito if name == "cognito-idp" else real(name, *args, **kwargs)
            ),
        )
        yield module


def account() -> dict[str, str]:
    boto3.client("secretsmanager").create_secret(Name=SECRET_ID)
    for name in PARAMETERS:
        boto3.client("ssm").put_parameter(Name=name, Value="UNSET", Type="String")
    return {"UserPoolId": POOL, "ClientId": CLIENT, "ClientSecret": CLIENT_SECRET}


def event(request_type: str, ids: dict[str, str], **overrides: Any) -> dict[str, Any]:
    properties = {
        "ServiceToken": "arn:aws:lambda:us-east-1:000000000000:function:registrar",
        "UserPoolId": ids["UserPoolId"],
        "ClientId": ids["ClientId"],
        "SecretId": SECRET_ID,
        "TokenUrl": TOKEN_URL,
        "MirrorUrl": MIRROR_URL,
        "Parameters": PARAMETERS,
        **overrides,
    }
    return {
        "RequestType": request_type,
        "ResponseURL": "https://cloudformation.example.invalid/response",
        "StackId": "stack",
        "RequestId": "request",
        "LogicalResourceId": "Registration",
        "ResourceProperties": properties,
    }


def run(module: Any, payload: dict[str, Any]) -> dict[str, Any]:
    sent: list[dict[str, Any]] = []
    module.handler(payload, None, send=lambda url, body: sent.append({"url": url, **body}))
    [response] = sent
    return response


def stored() -> dict[str, str]:
    value = boto3.client("secretsmanager").get_secret_value(SecretId=SECRET_ID)["SecretString"]
    return dict(json.loads(value))


def parameters() -> list[str]:
    ssm = boto3.client("ssm")
    return [ssm.get_parameter(Name=name)["Parameter"]["Value"] for name in PARAMETERS]


@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_ir_02_registration_stores_the_client_and_the_mirror_url(
    registrar: Any, request_type: str, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = account()

    response = run(registrar, event(request_type, ids))

    assert response["Status"] == "SUCCESS"
    assert response["url"] == "https://cloudformation.example.invalid/response"
    assert response["PhysicalResourceId"] == "aera-mirror-registration"
    assert stored() == {
        "clientId": ids["ClientId"],
        "clientSecret": ids["ClientSecret"],
        "tokenUrl": TOKEN_URL,
    }
    assert parameters() == [MIRROR_URL, MIRROR_URL]
    output = capsys.readouterr()
    assert ids["ClientSecret"] not in output.out + output.err + json.dumps(response)


def test_delete_leaves_the_registration_in_place(registrar: Any) -> None:
    ids = account()
    run(registrar, event("Create", ids))

    response = run(registrar, event("Delete", ids))

    assert response["Status"] == "SUCCESS"
    assert stored()["clientId"] == ids["ClientId"]
    assert parameters() == [MIRROR_URL, MIRROR_URL]


@pytest.mark.parametrize(
    "overrides",
    [
        {"MirrorUrl": "http://abc123.execute-api.us-east-1.amazonaws.com"},
        {"MirrorUrl": "https://abc123.execute-api.us-east-1.amazonaws.com/path"},
        {"TokenUrl": "http://aera.auth.us-east-1.amazoncognito.com/oauth2/token"},
        {"ClientId": "unknown-client"},
        {"SecretId": "/aera/dev/sap/missing"},
    ],
)
def test_nfr_sec_03_failure_is_reported_without_values(
    registrar: Any, overrides: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    ids = account()

    response = run(registrar, event("Create", ids, **overrides))

    assert response["Status"] == "FAILED"
    assert response["Reason"].startswith("Mirror registration failed")
    output = capsys.readouterr()
    assert ids["ClientSecret"] not in output.out + output.err + json.dumps(response)
    assert parameters() == ["UNSET", "UNSET"]
