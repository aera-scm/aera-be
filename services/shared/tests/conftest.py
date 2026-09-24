"""DynamoDB tables for shared-core tests, built from the same specs as the data stack."""

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
