"""SAP sandbox key transfer to Secrets Manager (NFR-SEC-03, IR-01).

The key is read from the process environment only and written straight into
the existing secret container. It is never printed, returned or logged, on
success or failure. The value used here is synthetic.
"""

from typing import NoReturn

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_secretsmanager import SecretsManagerClient
from provision_secrets import KEY_VARIABLE, main

SYNTHETIC_VALUE = "synthetic-sandbox-value-for-tests"
DEFAULT_NAME = "/aera/dev/sap/sandbox-api-key"


def secrets_client(region: str = "us-east-1") -> SecretsManagerClient:
    return boto3.client(
        "secretsmanager", region_name=region, config=Config(signature_version=UNSIGNED)
    )


def no_aws(profile: str, region: str) -> NoReturn:
    raise AssertionError("AWS must not be contacted")


def run(client: SecretsManagerClient, *args: str) -> int:
    return main(
        ["--env", "dev", "--profile", "aera-test", *args], client_factory=lambda p, r: client
    )


def test_nfr_sec_03_key_goes_into_the_container_without_being_echoed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(KEY_VARIABLE, SYNTHETIC_VALUE)
    client = secrets_client()
    with Stubber(client) as stubber:
        stubber.add_response(
            "put_secret_value",
            {
                "ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:x",
                "VersionId": "v" * 32,
            },
            {"SecretId": DEFAULT_NAME, "SecretString": SYNTHETIC_VALUE},
        )

        code = run(client)

        stubber.assert_no_pending_responses()

    captured = capsys.readouterr()
    assert code == 0
    assert f"Secret {DEFAULT_NAME} updated" in captured.out
    assert SYNTHETIC_VALUE not in captured.out + captured.err
    assert "123456789012" not in captured.out + captured.err


def test_nfr_sec_03_approved_existing_secret_name_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(KEY_VARIABLE, SYNTHETIC_VALUE)
    client = secrets_client()
    with Stubber(client) as stubber:
        stubber.add_response(
            "put_secret_value",
            {},
            {"SecretId": "/approved/sap-sandbox", "SecretString": SYNTHETIC_VALUE},
        )

        code = run(client, "--secret-name", "/approved/sap-sandbox")

        stubber.assert_no_pending_responses()

    assert code == 0


@pytest.mark.parametrize("value", [None, "", "   "])
def test_nfr_sec_03_missing_key_is_refused_before_contacting_aws(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(KEY_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(KEY_VARIABLE, value)

    code = main(["--env", "dev", "--profile", "aera-test"], client_factory=no_aws)

    assert code == 1
    assert f"{KEY_VARIABLE} is not set" in capsys.readouterr().err


def test_nfr_sec_03_missing_container_fails_without_echoing_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(KEY_VARIABLE, SYNTHETIC_VALUE)
    client = secrets_client()
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "put_secret_value",
            service_error_code="ResourceNotFoundException",
            service_message=f"Secret {DEFAULT_NAME} not found",
        )

        code = run(client)

    captured = capsys.readouterr()
    assert code == 1
    assert "does not exist; deploy the data stack first" in captured.err
    assert SYNTHETIC_VALUE not in captured.out + captured.err


def test_nfr_sec_03_access_denied_reports_code_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(KEY_VARIABLE, SYNTHETIC_VALUE)
    client = secrets_client()
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "put_secret_value",
            service_error_code="AccessDeniedException",
            service_message="User arn:aws:iam::123456789012:user/test is not authorized",
        )

        code = run(client)

    err = capsys.readouterr().err
    assert code == 1
    assert "PutSecretValue failed with AccessDeniedException" in err
    assert "123456789012" not in err
    assert SYNTHETIC_VALUE not in err


@pytest.mark.parametrize("env_name", ["final", "prod"])
def test_provisioning_refuses_non_dev_environments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env_name: str
) -> None:
    monkeypatch.setenv(KEY_VARIABLE, SYNTHETIC_VALUE)

    code = main(["--env", env_name, "--profile", "aera-test"], client_factory=no_aws)

    assert code == 1
    assert "only 'dev'" in capsys.readouterr().err


def test_nfr_cmp_02_provisioning_refuses_client_in_another_region(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(KEY_VARIABLE, SYNTHETIC_VALUE)
    client = secrets_client("eu-west-1")
    with Stubber(client) as stubber:
        code = run(client)

        stubber.assert_no_pending_responses()

    captured = capsys.readouterr()
    assert code == 1
    assert "not 'us-east-1'" in captured.err
    assert SYNTHETIC_VALUE not in captured.out + captured.err


def test_nfr_sec_03_key_is_not_accepted_as_a_command_line_argument() -> None:
    with pytest.raises(SystemExit):
        main(["--env", "dev", "--profile", "aera-test", "--value", SYNTHETIC_VALUE])
