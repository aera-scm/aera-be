"""The reference case gets its SRD id EXC-<year>-0914 in a fresh environment (SRD 6.6.3)."""

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws
from seed_config import SeedRefusedError, seed_case_counter

from services.shared.cases import CaseStore


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # pragma: allowlist secret
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName="aera-dev-cases",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield dynamodb


def test_the_first_case_after_seeding_is_the_reference_case(client: Any) -> None:
    assert seed_case_counter(client, env_name="dev", year=2026) is True

    assert CaseStore(client, "dev").next_case_id(2026) == "EXC-2026-0914"


def test_a_running_counter_is_never_reset(client: Any) -> None:
    store = CaseStore(client, "dev")
    seed_case_counter(client, env_name="dev", year=2026)
    store.next_case_id(2026)

    assert seed_case_counter(client, env_name="dev", year=2026) is False
    assert store.next_case_id(2026) == "EXC-2026-0915"


def test_only_dev_can_be_seeded(client: Any) -> None:
    with pytest.raises(SeedRefusedError):
        seed_case_counter(client, env_name="final", year=2026)
