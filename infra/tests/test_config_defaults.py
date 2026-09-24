"""SRD 6.23 configuration defaults and idempotent seeding (DR-10, NFR-MNT-02).

Seeding must never overwrite a value an administrator already changed, so every
write is conditional on the key being absent.
"""

from decimal import Decimal
from typing import Any

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import ANY, Stubber
from mypy_boto3_dynamodb import DynamoDBClient
from seed_config import SeedRefusedError, approver_items, config_items, main, rate_card_items, seed

from infra.config_defaults import (
    APPROVER_LIMITS,
    CONFIG_DEFAULTS,
    RATE_CARD,
    SSM_PARAMETER_KEYS,
)

# SRD 6.23, written out independently of the implementation.
EXPECTED_DEFAULTS: dict[str, Any] = {
    "TIER1_MAX_USD": Decimal("25000"),
    "TIER1_MIN_CONFIDENCE": Decimal("0.85"),
    "CRITICAL_FIELD_MIN_CONF": Decimal("0.95"),
    "GROUNDING_MIN": Decimal("0.7"),
    "AUDIT_SAMPLE_RATE": Decimal("0.2"),
    "MAX_ITERATIONS": Decimal("20"),
    "MAX_TOKENS": Decimal("150000"),
    "MAX_RUN_SECONDS": Decimal("300"),
    "DONOR_MIN_COVER_DAYS": Decimal("2.0"),
    "REOPEN_GRACE_HOURS": Decimal("2"),
    "APPROVAL_MAX_HOURS": Decimal("4"),
    "APPROVAL_REMINDER_AT": Decimal("0.5"),
    "SUPPLIER_REPLY_TIMEOUT_HOURS": Decimal("4"),
    "MRP_TOLERANCE_IN_DAYS": Decimal("3"),
    "MRP_TOLERANCE_OUT_DAYS": Decimal("15"),
    "KILL_SWITCH": "off",
}


def test_srd_6_23_config_table_defaults_match_the_catalogue() -> None:
    assert CONFIG_DEFAULTS == EXPECTED_DEFAULTS


def test_srd_6_23_scenario_t0_is_set_by_reset_not_seeded() -> None:
    assert "SCENARIO_T0" not in CONFIG_DEFAULTS


def test_srd_6_23_deployment_keys_live_in_ssm_not_the_config_table() -> None:
    assert set(SSM_PARAMETER_KEYS) == {
        "MODEL_SUPERVISOR_ID",
        "MODEL_SMALL_ID",
        "GUARDRAIL_ID",
        "GUARDRAIL_VERSION",
        "AR_POLICY_ARN",
        "SAP_READ_BASE",
        "SAP_WRITE_BASE",
        "SAP_SANDBOX_BASE",
    }
    assert not set(SSM_PARAMETER_KEYS) & set(CONFIG_DEFAULTS)


def test_dr_10_config_items_use_the_srd_key_layout() -> None:
    items = config_items(changed_at="2026-09-23T00:00:00Z")

    assert len(items) == len(EXPECTED_DEFAULTS)
    first = items[0]
    assert first == {
        "PK": {"S": "CFG#APPROVAL_MAX_HOURS"},
        "key": {"S": "APPROVAL_MAX_HOURS"},
        "value": {"N": "4"},
        "changedBy": {"S": "seed"},
        "changedAt": {"S": "2026-09-23T00:00:00Z"},
    }
    kill = next(item for item in items if item["key"]["S"] == "KILL_SWITCH")
    assert kill["value"] == {"S": "off"}


# Rate card and approver limits ----------------------------------------------


def test_dr_11_rate_card_prices_the_reference_options() -> None:
    by_type = {entry["actionType"]: entry for entry in RATE_CARD}

    sto = by_type["STO"]
    assert (sto["fromPlant"], sto["toPlant"]) == ("1020", "1010")
    assert (sto["fixedCostUsd"], sto["unitCostUsd"], sto["leadTimeHours"]) == (
        Decimal("4100"),
        Decimal("0"),
        Decimal("5"),
    )
    air = by_type["AIR_FREIGHT"]
    assert air["supplierId"] == "1000234"
    assert (air["fixedCostUsd"], air["leadTimeHours"]) == (Decimal("38200"), Decimal("17"))
    alternate = by_type["ALTERNATE_SUPPLIER"]
    assert alternate["supplierId"] == "1000871"
    assert alternate["unitCostUsd"] * 800 + alternate["fixedCostUsd"] == Decimal("51900")
    assert sto["fixedCostUsd"] + air["fixedCostUsd"] == Decimal("42300")


def test_dr_12_approvers_cover_the_reference_plan_with_a_backup() -> None:
    approvers = [entry for entry in APPROVER_LIMITS if entry["role"] == "approver"]

    assert len(approvers) >= 2, "an approver and a backup approver (BR-23)"
    assert all(entry["limitUsd"] >= Decimal("50000") for entry in approvers)  # OI-06
    assert all(entry["plant"] == "1010" for entry in approvers)
    assert all(entry["userId"].endswith(".example") for entry in approvers)


def test_dr_11_dr_12_items_use_the_srd_key_layout() -> None:
    rate = rate_card_items(changed_at="2026-09-23T00:00:00Z")[0]
    approver = approver_items(changed_at="2026-09-23T00:00:00Z")[0]

    assert rate["PK"]["S"] == f"RATE#{RATE_CARD[0]['entryId']}"
    assert rate["fixedCostUsd"] == {"N": str(RATE_CARD[0]["fixedCostUsd"])}
    assert rate["changedBy"] == {"S": "seed"}
    assert approver["PK"]["S"] == f"APPR#{APPROVER_LIMITS[0]['userId']}"
    assert approver["limitUsd"] == {"N": str(APPROVER_LIMITS[0]["limitUsd"])}
    assert approver["grantedBy"] == {"S": "seed"}


# Seeding ----------------------------------------------------------------------

ALL_KEYS = (
    [f"CFG#{key}" for key in sorted(EXPECTED_DEFAULTS)]
    + [f"RATE#{entry['entryId']}" for entry in RATE_CARD]
    + [f"APPR#{entry['userId']}" for entry in APPROVER_LIMITS]
)


def dynamodb_client(region: str = "us-east-1") -> DynamoDBClient:
    return boto3.client("dynamodb", region_name=region, config=Config(signature_version=UNSIGNED))


EXPECTED_PUT = {
    "TableName": "aera-dev-config",
    "Item": ANY,
    "ConditionExpression": "attribute_not_exists(PK)",
}


def stub_puts(stubber: Stubber, existing: set[str]) -> None:
    for key in ALL_KEYS:
        if key in existing:
            stubber.add_client_error(
                "put_item",
                service_error_code="ConditionalCheckFailedException",
                expected_params=EXPECTED_PUT,
            )
        else:
            stubber.add_response("put_item", {}, EXPECTED_PUT)


def test_nfr_mnt_02_first_seed_writes_every_default_rate_and_limit() -> None:
    client = dynamodb_client()
    with Stubber(client) as stubber:
        stub_puts(stubber, existing=set())

        result = seed(client, env_name="dev", changed_at="2026-09-23T00:00:00Z")

        stubber.assert_no_pending_responses()

    assert result.written == ALL_KEYS
    assert result.preserved == []


def test_nfr_mnt_02_reseeding_preserves_administrator_changes() -> None:
    client = dynamodb_client()
    changed = {
        "CFG#TIER1_MAX_USD",
        "CFG#KILL_SWITCH",
        ALL_KEYS[-1],
        f"RATE#{RATE_CARD[0]['entryId']}",
    }
    with Stubber(client) as stubber:
        stub_puts(stubber, existing=changed)

        result = seed(client, env_name="dev", changed_at="2026-09-23T00:00:00Z")

        stubber.assert_no_pending_responses()

    assert sorted(result.preserved) == sorted(changed)
    assert result.written == [key for key in ALL_KEYS if key not in changed]


def test_nfr_mnt_02_second_seed_changes_nothing() -> None:
    client = dynamodb_client()
    with Stubber(client) as stubber:
        stub_puts(stubber, existing=set(ALL_KEYS))

        result = seed(client, env_name="dev", changed_at="2026-09-23T00:00:00Z")

    assert result.written == []
    assert result.preserved == ALL_KEYS


def test_seed_stops_on_other_api_errors_with_code_only() -> None:
    client = dynamodb_client()
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "put_item",
            service_error_code="AccessDeniedException",
            service_message="User arn:aws:iam::123456789012:user/test is not authorized",
        )

        with pytest.raises(SeedRefusedError) as error:
            seed(client, env_name="dev", changed_at="2026-09-23T00:00:00Z")

    assert "PutItem failed with AccessDeniedException" in str(error.value)
    assert "123456789012" not in str(error.value)


@pytest.mark.parametrize("env_name", ["final", "prod"])
def test_seed_refuses_non_dev_environments(env_name: str) -> None:
    with pytest.raises(SeedRefusedError, match="only 'dev'"):
        seed(dynamodb_client(), env_name=env_name, changed_at="2026-09-23T00:00:00Z")


def test_seed_refuses_client_outside_approved_region() -> None:
    with pytest.raises(SeedRefusedError, match="not the approved region"):
        seed(dynamodb_client("eu-west-1"), env_name="dev", changed_at="2026-09-23T00:00:00Z")


def test_seed_cli_reports_counts(capsys: pytest.CaptureFixture[str]) -> None:
    client = dynamodb_client()
    with Stubber(client) as stubber:
        stub_puts(stubber, existing={"CFG#TIER1_MAX_USD"})

        code = main(["--env", "dev", "--profile", "aera-test"], client_factory=lambda p, r: client)

    out = capsys.readouterr().out
    assert code == 0
    assert f"{len(ALL_KEYS) - 1} items written, 1 existing value preserved" in out
