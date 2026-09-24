"""AWS fakes for service tests: tables built from the same specs as the data stack, bucket, bus."""

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from infra.stacks.data import TABLES

ENV = "test"


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # pragma: allowlist secret
    monkeypatch.setenv("AERA_ENV", ENV)
    with mock_aws():
        yield


@pytest.fixture
def dynamodb(aws: None) -> Any:
    client = boto3.client("dynamodb", region_name="us-east-1")
    for spec in TABLES:
        attributes = {"PK": "S"}
        keys = [{"AttributeName": "PK", "KeyType": "HASH"}]
        if spec.sort_key:
            attributes["SK"] = "S"
            keys.append({"AttributeName": "SK", "KeyType": "RANGE"})
        indexes = []
        for index in spec.indexes:
            index_keys = [{"AttributeName": index.partition[0], "KeyType": "HASH"}]
            attributes[index.partition[0]] = "S" if index.partition[1].value == "S" else "N"
            if index.sort:
                index_keys.append({"AttributeName": index.sort[0], "KeyType": "RANGE"})
                attributes[index.sort[0]] = "S" if index.sort[1].value == "S" else "N"
            indexes.append(
                {
                    "IndexName": index.name,
                    "KeySchema": index_keys,
                    "Projection": {"ProjectionType": "ALL"},
                }
            )
        arguments: dict[str, Any] = {
            "TableName": f"aera-{ENV}-{spec.name}",
            "KeySchema": keys,
            "AttributeDefinitions": [
                {"AttributeName": name, "AttributeType": kind} for name, kind in attributes.items()
            ],
            "BillingMode": "PAY_PER_REQUEST",
        }
        if indexes:
            arguments["GlobalSecondaryIndexes"] = indexes
        client.create_table(**arguments)
    return client


RAW_BUCKET = "aera-test-raw"


@pytest.fixture
def s3(aws: None) -> Any:
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=RAW_BUCKET)
    return client


class RecordingBus:
    """EventBridge stand-in that keeps what was published, in order."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def put_events(self, Entries: list[dict[str, Any]]) -> dict[str, Any]:
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "x"} for _ in Entries]}

    def types(self) -> list[str]:
        return [entry["DetailType"] for entry in self.entries]

    def details(self, detail_type: str) -> list[dict[str, Any]]:
        import json

        return [json.loads(e["Detail"]) for e in self.entries if e["DetailType"] == detail_type]


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture(scope="session")
def mirror_url() -> Iterator[str]:
    from mirror_process import AVAILABLE, MISSING, running_mirror

    if not AVAILABLE:
        pytest.skip(MISSING)
    with running_mirror() as url:
        yield url


@pytest.fixture
def sap(mirror_url: str) -> Any:
    from services.shared.sap_client import Endpoint, SapClient, Target

    endpoint = Endpoint(base_url=mirror_url, target=Target.MIRROR, auth=None)
    return SapClient(read=endpoint, write=None)
