"""Data stack synthesis (SRD 6.16, 6.20, 6.23; NFR-SEC-03, NFR-SEC-05, NFR-CMP-02, NFR-MNT-02).

Expected keys, indexes, TTLs and streams are written out from SRD 6.20 here
rather than imported from the stack, so the test cannot agree with a wrong
implementation by construction.
"""

from typing import Any

import pytest
from aws_cdk import Stack, assertions

from infra.app import DataSettings, build_app, data_settings_from_environment
from infra.environments import EnvironmentRefusedError
from infra.stacks.data import SAP_SANDBOX_BASE_URL, UNSET

SETTINGS = DataSettings(env_name="dev", owner="aera-test-owner")

# SRD 6.20: table -> (partition key, sort key or None)
KEYS: dict[str, tuple[str, str | None]] = {
    "cases": ("PK", "SK"),
    "signals": ("PK", "SK"),
    "trace": ("PK", "SK"),
    "ledger": ("PK", "SK"),
    "idempotency": ("PK", None),
    "audit": ("PK", "SK"),
    "dialogue": ("PK", "SK"),
    "analytics": ("PK", "SK"),
    "config": ("PK", None),
    "connections": ("PK", None),
}
# SRD 6.20: table -> {index name: (partition attr, type), (sort attr, type) or None}
GSIS: dict[str, dict[str, tuple[tuple[str, str], tuple[str, str] | None]]] = {
    "cases": {
        "GSI1": (("status", "S"), ("priorityScore", "N")),
        "GSI2": (("approverId", "S"), ("routedAt", "S")),
    },
    "signals": {
        "GSI1": (("caseId", "S"), ("receivedAt", "S")),
        "GSI2": (("poNumber", "S"), ("receivedAt", "S")),
    },
    "dialogue": {"GSI1": (("referenceToken", "S"), None)},
    "connections": {"GSI1": (("caseId", "S"), None)},
}
TTL_TABLES = {"trace", "idempotency", "connections"}
STREAMS = {"cases": "NEW_AND_OLD_IMAGES", "trace": "NEW_IMAGE", "audit": "NEW_IMAGE"}
SSM_KEYS = {
    "MODEL_SUPERVISOR_ID",
    "MODEL_SMALL_ID",
    "GUARDRAIL_ID",
    "GUARDRAIL_VERSION",
    "AR_POLICY_ARN",
    "SAP_READ_BASE",
    "SAP_WRITE_BASE",
    "SAP_SANDBOX_BASE",
}
REQUIRED_TAGS = {"project": "aera", "env": "dev", "component": "data", "owner": "aera-test-owner"}


def template(settings: DataSettings = SETTINGS) -> assertions.Template:
    app = build_app(settings)
    return assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-data")))


@pytest.fixture(scope="module")
def data() -> assertions.Template:
    return template()


def tables(data: assertions.Template) -> dict[str, Any]:
    found = data.find_resources("AWS::DynamoDB::Table")
    return {
        props["Properties"]["TableName"].removeprefix("aera-dev-"): props
        for props in found.values()
    }


def logical_id(data: assertions.Template, resource_type: str) -> str:
    ids = list(data.find_resources(resource_type))
    assert len(ids) == 1
    return ids[0]


# Tables -----------------------------------------------------------------------


def test_srd_6_20_exactly_ten_physical_tables(data: assertions.Template) -> None:
    assert sorted(tables(data)) == sorted(KEYS)


@pytest.mark.parametrize("name", sorted(KEYS))
def test_srd_6_20_table_keys(data: assertions.Template, name: str) -> None:
    props = tables(data)[name]["Properties"]
    partition, sort = KEYS[name]
    expected = [{"AttributeName": partition, "KeyType": "HASH"}]
    if sort:
        expected.append({"AttributeName": sort, "KeyType": "RANGE"})

    assert props["KeySchema"] == expected


@pytest.mark.parametrize("name", sorted(KEYS))
def test_srd_6_20_table_indexes(data: assertions.Template, name: str) -> None:
    props = tables(data)[name]["Properties"]
    actual = {}
    types = {a["AttributeName"]: a["AttributeType"] for a in props["AttributeDefinitions"]}
    for index in props.get("GlobalSecondaryIndexes", []):
        schema = {k["KeyType"]: k["AttributeName"] for k in index["KeySchema"]}
        sort = schema.get("RANGE")
        actual[index["IndexName"]] = (
            (schema["HASH"], types[schema["HASH"]]),
            (sort, types[sort]) if sort else None,
        )
        assert index["Projection"] == {"ProjectionType": "ALL"}

    assert actual == GSIS.get(name, {})


@pytest.mark.parametrize("name", sorted(KEYS))
def test_srd_6_20_ttl_only_where_specified(data: assertions.Template, name: str) -> None:
    ttl = tables(data)[name]["Properties"].get("TimeToLiveSpecification")

    if name in TTL_TABLES:
        assert ttl == {"AttributeName": "ttl", "Enabled": True}
    else:
        assert ttl is None


@pytest.mark.parametrize("name", sorted(KEYS))
def test_srd_6_20_streams_only_where_specified(data: assertions.Template, name: str) -> None:
    stream = tables(data)[name]["Properties"].get("StreamSpecification")

    if name in STREAMS:
        assert stream == {"StreamViewType": STREAMS[name]}
    else:
        assert stream is None


@pytest.mark.parametrize("name", sorted(KEYS))
def test_nfr_sec_05_table_on_demand_pitr_kms_protected(
    data: assertions.Template, name: str
) -> None:
    resource = tables(data)[name]
    props = resource["Properties"]
    key_id = logical_id(data, "AWS::KMS::Key")

    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert props["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True
    assert props["SSESpecification"]["SSEEnabled"] is True
    assert props["SSESpecification"]["SSEType"] == "KMS"
    assert props["SSESpecification"]["KMSMasterKeyId"] == {"Fn::GetAtt": [key_id, "Arn"]}
    assert props["DeletionProtectionEnabled"] is True
    assert resource["DeletionPolicy"] == "Retain"


# Buckets ----------------------------------------------------------------------


def buckets(data: assertions.Template) -> dict[str, Any]:
    found = data.find_resources("AWS::S3::Bucket")
    named: dict[str, Any] = {}
    for resource in found.values():
        name = resource["Properties"]["BucketName"]["Fn::Join"][1][0]
        named[name.removeprefix("aera-dev-").rstrip("-")] = resource
    return named


def test_srd_6_20_raw_artefacts_and_audit_buckets_only(data: assertions.Template) -> None:
    # The web bucket belongs to the web stack (plan step 6).
    assert sorted(buckets(data)) == ["artefacts", "audit", "raw"]


def test_nfr_mnt_02_bucket_names_are_unique_per_account_and_region(
    data: assertions.Template,
) -> None:
    for resource in buckets(data).values():
        parts = resource["Properties"]["BucketName"]["Fn::Join"][1]
        assert {"Ref": "AWS::AccountId"} in parts
        assert {"Ref": "AWS::Region"} in parts or "us-east-1" in "".join(
            p for p in parts if isinstance(p, str)
        )


@pytest.mark.parametrize("name", ["raw", "artefacts", "audit"])
def test_nfr_sec_05_bucket_kms_encrypted_private_and_retained(
    data: assertions.Template, name: str
) -> None:
    resource = buckets(data)[name]
    props = resource["Properties"]
    key_id = logical_id(data, "AWS::KMS::Key")
    rule = props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]

    assert rule["ServerSideEncryptionByDefault"] == {
        "SSEAlgorithm": "aws:kms",
        "KMSMasterKeyID": {"Fn::GetAtt": [key_id, "Arn"]},
    }
    assert rule["BucketKeyEnabled"] is True
    assert props["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert resource["DeletionPolicy"] == "Retain"


def bucket_policy_statements(data: assertions.Template, bucket_id: str) -> list[dict[str, Any]]:
    for policy in data.find_resources("AWS::S3::BucketPolicy").values():
        if policy["Properties"]["Bucket"] == {"Ref": bucket_id}:
            statements: list[dict[str, Any]] = policy["Properties"]["PolicyDocument"]["Statement"]
            return statements
    raise AssertionError(f"no bucket policy for {bucket_id}")


@pytest.mark.parametrize("name", ["raw", "artefacts", "audit"])
def test_nfr_sec_05_bucket_denies_plain_http_and_tls_below_1_2(
    data: assertions.Template, name: str
) -> None:
    ids = {
        props["Properties"]["BucketName"]["Fn::Join"][1][0]: logical
        for logical, props in data.find_resources("AWS::S3::Bucket").items()
    }
    statements = bucket_policy_statements(data, ids[f"aera-dev-{name}-"])
    conditions = [s["Condition"] for s in statements if s["Effect"] == "Deny"]

    assert {"Bool": {"aws:SecureTransport": "false"}} in conditions
    assert {"NumericLessThan": {"s3:TlsVersion": 1.2}} in conditions


def test_srd_6_20_raw_bucket_expires_objects_after_90_days(data: assertions.Template) -> None:
    rules = buckets(data)["raw"]["Properties"]["LifecycleConfiguration"]["Rules"]

    assert [{"ExpirationInDays": 90, "Status": "Enabled"}] == [
        {k: v for k, v in rule.items() if k in {"ExpirationInDays", "Status"}} for rule in rules
    ]


def test_srd_6_20_audit_bucket_object_lock_compliance_90_days(
    data: assertions.Template,
) -> None:
    props = buckets(data)["audit"]["Properties"]

    assert props["ObjectLockEnabled"] is True
    assert props["ObjectLockConfiguration"] == {
        "ObjectLockEnabled": "Enabled",
        "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 90}},
    }
    assert props["VersioningConfiguration"] == {"Status": "Enabled"}


# Key, bus, parameters, secrets ------------------------------------------------


def test_nfr_sec_05_project_kms_key_rotates_and_is_retained(data: assertions.Template) -> None:
    key = data.find_resources("AWS::KMS::Key")[logical_id(data, "AWS::KMS::Key")]

    assert key["Properties"]["EnableKeyRotation"] is True
    assert key["DeletionPolicy"] == "Retain"
    data.has_resource_properties("AWS::KMS::Alias", {"AliasName": "alias/aera-dev"})


def test_srd_6_16_event_bus_is_named_aera_env(data: assertions.Template) -> None:
    data.resource_count_is("AWS::Events::EventBus", 1)
    data.has_resource_properties("AWS::Events::EventBus", {"Name": "aera-dev"})


def ssm_values(data: assertions.Template) -> dict[str, Any]:
    return {
        p["Properties"]["Name"]: p["Properties"]["Value"]
        for p in data.find_resources("AWS::SSM::Parameter").values()
    }


def test_srd_6_23_deployment_parameters_exist_under_aera_env(data: assertions.Template) -> None:
    assert set(ssm_values(data)) == {f"/aera/dev/{key}" for key in SSM_KEYS}


def test_srd_6_23_unprovisioned_parameters_are_explicitly_unset(
    data: assertions.Template,
) -> None:
    values = ssm_values(data)

    assert UNSET == "UNSET"
    for key in SSM_KEYS - {"SAP_SANDBOX_BASE"}:
        assert values[f"/aera/dev/{key}"] == UNSET
    assert values["/aera/dev/SAP_SANDBOX_BASE"] == SAP_SANDBOX_BASE_URL
    assert SAP_SANDBOX_BASE_URL == "https://sandbox.api.sap.com/s4hanacloud"


def test_a_01_approved_model_ids_are_written_when_supplied() -> None:
    settings = DataSettings(
        env_name="dev",
        owner="aera-test-owner",
        model_supervisor_id="anthropic.claude-sonnet-4-5-20250929-v1:0",
        model_small_id="amazon.nova-lite-v1:0",
    )

    values = ssm_values(template(settings))

    assert values["/aera/dev/MODEL_SUPERVISOR_ID"] == "anthropic.claude-sonnet-4-5-20250929-v1:0"
    assert values["/aera/dev/MODEL_SMALL_ID"] == "amazon.nova-lite-v1:0"


def secrets(data: assertions.Template) -> dict[str, Any]:
    return {
        s["Properties"]["Name"]: s
        for s in data.find_resources("AWS::SecretsManager::Secret").values()
    }


def test_nfr_sec_03_empty_secret_containers_for_sandbox_and_mirror(
    data: assertions.Template,
) -> None:
    found = secrets(data)
    key_id = logical_id(data, "AWS::KMS::Key")

    assert sorted(found) == ["/aera/dev/sap/mirror-oauth-client", "/aera/dev/sap/sandbox-api-key"]
    for secret in found.values():
        props = secret["Properties"]
        assert "SecretString" not in props
        assert "GenerateSecretString" not in props
        assert props["KmsKeyId"] == {"Fn::GetAtt": [key_id, "Arn"]}
        assert secret["DeletionPolicy"] == "Retain"


def test_nfr_sec_03_existing_approved_secret_is_referenced_not_duplicated() -> None:
    settings = DataSettings(
        env_name="dev", owner="aera-test-owner", sandbox_secret_name="/approved/sap-sandbox"
    )

    assert sorted(secrets(template(settings))) == ["/aera/dev/sap/mirror-oauth-client"]


def test_nfr_sec_03_no_secret_value_in_template(data: assertions.Template) -> None:
    text = str(data.to_json())

    assert "SecretString" not in text
    assert "GenerateSecretString" not in text


# Stack-wide -------------------------------------------------------------------

TAGGED_TYPES = {
    "AWS::DynamoDB::Table",
    "AWS::S3::Bucket",
    "AWS::KMS::Key",
    "AWS::Events::EventBus",
    "AWS::SSM::Parameter",
    "AWS::SecretsManager::Secret",
}


def resource_tags(resource: dict[str, Any]) -> dict[str, str]:
    tags = resource["Properties"].get("Tags", {})
    if isinstance(tags, dict):
        return dict(tags)
    return {tag["Key"]: tag["Value"] for tag in tags}


def test_srd_6_16_every_taggable_resource_carries_required_tags(
    data: assertions.Template,
) -> None:
    resources = data.to_json()["Resources"]
    tagged = [r for r in resources.values() if r["Type"] in TAGGED_TYPES]

    assert len(tagged) == 10 + 3 + 1 + 1 + 8 + 2
    for resource in tagged:
        assert REQUIRED_TAGS.items() <= resource_tags(resource).items(), resource["Type"]


def test_nfr_cmp_02_stack_is_pinned_to_the_approved_region() -> None:
    app = build_app(SETTINGS)
    stack = Stack.of(app.node.find_child("aera-dev-data"))

    assert stack.region == "us-east-1"


def test_srd_6_16_data_stack_has_no_compute_or_custom_resources(
    data: assertions.Template,
) -> None:
    types = {r["Type"] for r in data.to_json()["Resources"].values()}

    assert not any(t.startswith(("AWS::Lambda::", "Custom::", "AWS::IAM::Role")) for t in types)


def test_srd_6_16_app_holds_only_the_data_stack_for_now() -> None:
    assembly = build_app(SETTINGS).synth()

    assert [stack.stack_name for stack in assembly.stacks] == ["aera-dev-data"]


@pytest.mark.parametrize("env_name", ["final", "prod", ""])
def test_app_refuses_non_dev_environments(env_name: str) -> None:
    with pytest.raises(EnvironmentRefusedError):
        build_app(DataSettings(env_name=env_name, owner="aera-test-owner"))


def test_data_settings_are_read_from_environment() -> None:
    settings = data_settings_from_environment(
        {
            "AERA_ENV": "dev",
            "AERA_OWNER_TAG": "aera-test-owner",
            "MODEL_SUPERVISOR_ID": "anthropic.claude-sonnet-4-5-20250929-v1:0",
            "MODEL_SMALL_ID": " ",
            "AERA_SAP_SANDBOX_SECRET_NAME": "/approved/sap-sandbox",
        }
    )

    assert settings == DataSettings(
        env_name="dev",
        owner="aera-test-owner",
        model_supervisor_id="anthropic.claude-sonnet-4-5-20250929-v1:0",
        model_small_id=None,
        sandbox_secret_name="/approved/sap-sandbox",
        mirror_secret_name=None,
    )


def test_srd_6_16_owner_tag_is_required() -> None:
    with pytest.raises(ValueError, match="AERA_OWNER_TAG"):
        data_settings_from_environment({"AERA_ENV": "dev"})
