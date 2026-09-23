"""Offline region and model-id configuration checks (OI-04, A-01, NFR-CMP-02).

No AWS call is made here. A passing configuration check is not account or
invocation evidence; ADR-001 stays proposed until OT-01 and OT-03 are verified.
"""

from typing import NoReturn

import pytest
from bedrock_stubs import SMALL_MODEL_ID, SUPERVISOR_MODEL_ID
from check_model_access import main as model_main
from check_model_access import model_id_problems
from check_region import APPROVED_REGION, region_problems
from check_region import main as region_main

from infra import environments


def no_aws(profile: str, region: str) -> NoReturn:
    raise AssertionError("AWS must not be contacted")


# Region ---------------------------------------------------------------------


def test_nfr_cmp_02_approved_region_is_us_east_1() -> None:
    assert APPROVED_REGION == environments.APPROVED_REGION == "us-east-1"
    assert region_problems("us-east-1", {}) == []


@pytest.mark.parametrize("region", ["eu-central-1", "us-west-2", "ap-southeast-3"])
def test_nfr_cmp_02_other_region_is_rejected(region: str) -> None:
    problems = region_problems(region, {})

    assert len(problems) == 1
    assert f"region '{region}' is not the approved region 'us-east-1'" in problems[0]
    assert "ADR-001" in problems[0]


@pytest.mark.parametrize("region", ["", "US-EAST-1", "us-east", "useast1"])
def test_nfr_cmp_02_malformed_region_is_rejected(region: str) -> None:
    assert any("not a valid AWS region" in p for p in region_problems(region, {}))


@pytest.mark.parametrize("variable", ["AWS_REGION", "AWS_DEFAULT_REGION"])
def test_nfr_cmp_02_mismatched_sdk_region_is_rejected(variable: str) -> None:
    problems = region_problems("us-east-1", {variable: "us-west-2"})

    assert problems == [f"{variable} is 'us-west-2' but the deployment region is 'us-east-1'"]


def test_nfr_cmp_02_matching_sdk_region_is_accepted() -> None:
    environ = {"AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1"}

    assert region_problems("us-east-1", environ) == []


def test_check_region_offline_run_reports_configuration_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = region_main(["--region", "us-east-1"], clients_factory=no_aws)

    out = capsys.readouterr().out
    assert code == 0
    assert "Offline configuration: region us-east-1 is the approved region" in out
    assert "not account evidence" in out


def test_check_region_offline_run_rejects_unapproved_region(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = region_main(["--region", "eu-west-1"], clients_factory=no_aws)

    assert code == 1
    assert "not the approved region" in capsys.readouterr().err


# Model ids ------------------------------------------------------------------


def test_a_01_direct_regional_model_ids_are_accepted() -> None:
    assert model_id_problems(SUPERVISOR_MODEL_ID, SMALL_MODEL_ID) == []


def test_a_01_small_claude_model_is_accepted_as_small_model() -> None:
    assert model_id_problems(SUPERVISOR_MODEL_ID, "anthropic.claude-haiku-4-5-20251001-v1:0") == []


@pytest.mark.parametrize("prefix", ["us", "eu", "apac", "global", "us-gov", "jp", "au", "ca"])
def test_a_01_geographic_or_global_inference_profile_is_rejected(prefix: str) -> None:
    problems = model_id_problems(f"{prefix}.{SUPERVISOR_MODEL_ID}", SMALL_MODEL_ID)

    assert len(problems) == 1
    assert "MODEL_SUPERVISOR_ID" in problems[0]
    assert "inference profile" in problems[0]
    assert "A-01" in problems[0]


def test_a_01_unknown_three_part_id_is_treated_as_inference_profile() -> None:
    problems = model_id_problems(SUPERVISOR_MODEL_ID, f"xx.{SMALL_MODEL_ID}")

    assert len(problems) == 1
    assert "MODEL_SMALL_ID" in problems[0]
    assert "inference profile" in problems[0]


@pytest.mark.parametrize(
    "arn",
    [
        f"arn:aws:bedrock:us-east-1::foundation-model/{SUPERVISOR_MODEL_ID}",
        f"arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.{SUPERVISOR_MODEL_ID}",
        "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2c3d4e5f6",
    ],
)
def test_a_01_arn_is_rejected_in_favour_of_bare_model_id(arn: str) -> None:
    problems = model_id_problems(arn, SMALL_MODEL_ID)

    assert len(problems) == 1
    assert "bare model id" in problems[0]
    assert "123456789012" not in problems[0]


@pytest.mark.parametrize(
    ("supervisor", "small", "variable"),
    [("", SMALL_MODEL_ID, "MODEL_SUPERVISOR_ID"), (SUPERVISOR_MODEL_ID, "  ", "MODEL_SMALL_ID")],
)
def test_a_01_missing_model_id_is_never_inferred(
    supervisor: str, small: str, variable: str
) -> None:
    assert model_id_problems(supervisor, small) == [f"{variable} is required (OT-03)"]


@pytest.mark.parametrize(
    "supervisor", ["amazon.nova-pro-v1:0", "meta.llama3-70b-instruct-v1:0", "anthropic.titan-v1"]
)
def test_a_01_supervisor_must_be_a_claude_model(supervisor: str) -> None:
    problems = model_id_problems(supervisor, SMALL_MODEL_ID)

    assert len(problems) == 1
    assert "MODEL_SUPERVISOR_ID" in problems[0]
    assert "Anthropic Claude" in problems[0]


@pytest.mark.parametrize("small", ["meta.llama3-8b-instruct-v1:0", "amazon.titan-text-lite-v1"])
def test_a_01_small_model_must_be_nova_or_claude(small: str) -> None:
    problems = model_id_problems(SUPERVISOR_MODEL_ID, small)

    assert len(problems) == 1
    assert "MODEL_SMALL_ID" in problems[0]
    assert "Amazon Nova or Anthropic Claude" in problems[0]


@pytest.mark.parametrize(
    "model_id", ["claude sonnet", "anthropic.", "anthropic.claude-x:0:1:2", "Anthropic.Claude-v1"]
)
def test_a_01_malformed_model_id_is_rejected(model_id: str) -> None:
    problems = model_id_problems(model_id, SMALL_MODEL_ID)

    assert len(problems) == 1
    assert "not a Bedrock model id" in problems[0]


def test_check_model_access_offline_run_reads_srd_configuration_names(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MODEL_SUPERVISOR_ID", SUPERVISOR_MODEL_ID)
    monkeypatch.setenv("MODEL_SMALL_ID", SMALL_MODEL_ID)

    code = model_main([], clients_factory=no_aws)

    out = capsys.readouterr().out
    assert code == 0
    assert f"supervisor {SUPERVISOR_MODEL_ID}" in out
    assert f"small {SMALL_MODEL_ID}" in out
    assert "not account evidence" in out


def test_check_model_access_rejects_profile_before_contacting_aws(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = model_main(
        [
            "--supervisor-model-id",
            f"us.{SUPERVISOR_MODEL_ID}",
            "--small-model-id",
            SMALL_MODEL_ID,
            "--live",
            "--profile",
            "aera-test",
            "--budget-name",
            "aera-test-monthly",
        ],
        clients_factory=no_aws,
    )

    assert code == 1
    assert "inference profile" in capsys.readouterr().err


def test_check_model_access_rejects_unapproved_region_before_contacting_aws(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = model_main(
        [
            "--region",
            "eu-west-1",
            "--supervisor-model-id",
            SUPERVISOR_MODEL_ID,
            "--small-model-id",
            SMALL_MODEL_ID,
        ],
        clients_factory=no_aws,
    )

    assert code == 1
    assert "not the approved region" in capsys.readouterr().err
