"""Register the deployed SAP Mirror with AERA (SRD 6.16, IR-02, IR-03, NFR-SEC-03).

The Mirror URL goes to SSM as SAP_READ_BASE and SAP_WRITE_BASE; the OAuth client goes
straight from the process environment into its Secrets Manager container and is never
printed. All values here are synthetic.
"""

import json
from typing import Any, NoReturn

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from register_mirror import CLIENT_VARIABLES, main

URL = "https://aera-sap-mirror.cfapps.example"
SECRET = "synthetic-client-secret-for-tests"  # pragma: allowlist secret
CLIENT = {
    "MIRROR_CLIENT_ID": "synthetic-client-id",
    "MIRROR_CLIENT_SECRET": SECRET,
    "MIRROR_TOKEN_URL": "https://aera.authentication.example/oauth/token",
}


def clients(region: str = "us-east-1") -> tuple[Any, Any]:
    config = Config(signature_version=UNSIGNED)
    return (
        boto3.client("ssm", region_name=region, config=config),
        boto3.client("secretsmanager", region_name=region, config=config),
    )


def no_aws(profile: str, region: str) -> NoReturn:
    raise AssertionError("AWS must not be contacted")


@pytest.fixture
def client_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in CLIENT.items():
        monkeypatch.setenv(name, value)


def run(ssm: Any, secrets: Any, *args: str) -> int:
    return main(
        ["--env", "dev", "--profile", "aera-test", "--url", URL, *args],
        clients_factory=lambda p, r: (ssm, secrets),
    )


@pytest.mark.usefixtures("client_env")
def test_ir_03_mirror_url_becomes_read_and_write_base(capsys: pytest.CaptureFixture[str]) -> None:
    ssm, secrets = clients()
    with Stubber(ssm) as ssm_stub, Stubber(secrets) as secrets_stub:
        for key in ("SAP_READ_BASE", "SAP_WRITE_BASE"):
            ssm_stub.add_response(
                "put_parameter",
                {"Version": 2},
                {"Name": f"/aera/dev/{key}", "Value": URL, "Type": "String", "Overwrite": True},
            )
        secrets_stub.add_response(
            "put_secret_value",
            {},
            {
                "SecretId": "/aera/dev/sap/mirror-oauth-client",
                "SecretString": json.dumps(
                    {
                        "clientId": CLIENT["MIRROR_CLIENT_ID"],
                        "clientSecret": SECRET,
                        "tokenUrl": CLIENT["MIRROR_TOKEN_URL"],
                    },
                    sort_keys=True,
                ),
            },
        )

        code = run(ssm, secrets)

        ssm_stub.assert_no_pending_responses()
        secrets_stub.assert_no_pending_responses()

    captured = capsys.readouterr()
    assert code == 0
    assert "SAP_READ_BASE and SAP_WRITE_BASE set" in captured.out
    assert SECRET not in captured.out + captured.err


@pytest.mark.usefixtures("client_env")
@pytest.mark.parametrize("url", ["http://aera.example", "not a url", "https://", "https://x/y?z=1"])
def test_ir_03_only_a_plain_https_origin_is_accepted(
    url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--env", "dev", "--profile", "aera-test", "--url", url], clients_factory=no_aws)

    assert code == 1
    assert "https" in capsys.readouterr().err


@pytest.mark.parametrize("missing", CLIENT_VARIABLES)
def test_nfr_sec_03_incomplete_client_is_refused_before_aws(
    missing: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in CLIENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)

    code = main(["--env", "dev", "--profile", "aera-test", "--url", URL], clients_factory=no_aws)

    assert code == 1
    assert f"{missing} is not set" in capsys.readouterr().err


@pytest.mark.usefixtures("client_env")
def test_nfr_sec_03_failure_reports_code_only(capsys: pytest.CaptureFixture[str]) -> None:
    ssm, secrets = clients()
    with Stubber(ssm) as ssm_stub, Stubber(secrets):
        ssm_stub.add_client_error(
            "put_parameter",
            service_error_code="AccessDeniedException",
            service_message="User arn:aws:iam::123456789012:user/test is not authorized",
        )

        code = run(ssm, secrets)

    err = capsys.readouterr().err
    assert code == 1
    assert "PutParameter failed with AccessDeniedException" in err
    assert "123456789012" not in err and SECRET not in err


@pytest.mark.usefixtures("client_env")
@pytest.mark.parametrize("env_name", ["final", "prod"])
def test_registration_refuses_non_dev_environments(
    env_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--env", env_name, "--profile", "aera-test", "--url", URL], clients_factory=no_aws)

    assert code == 1
    assert "only 'dev'" in capsys.readouterr().err


@pytest.mark.usefixtures("client_env")
def test_nfr_cmp_02_clients_in_another_region_are_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ssm, secrets = clients("eu-west-1")
    with Stubber(ssm), Stubber(secrets):
        code = run(ssm, secrets)

    assert code == 1
    assert "not 'us-east-1'" in capsys.readouterr().err
