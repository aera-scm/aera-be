"""Configuration accessor (SRD 6.23, 60 s cache) and the event envelope publisher (6.17)."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
import pytest

from services.shared.config import Config
from services.shared.defaults import CONFIG_DEFAULTS
from services.shared.events import EventPublishError, publish
from services.shared.models import EventEnvelope


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def put(dynamodb: Any, key: str, value: dict[str, str]) -> None:
    dynamodb.put_item(
        TableName="aera-test-config",
        Item={"PK": {"S": f"CFG#{key}"}, "key": {"S": key}, "value": value},
    )


def test_srd_6_23_defaults_apply_until_an_administrator_changes_a_value(dynamodb: Any) -> None:
    config = Config(dynamodb, env="test")

    assert config.get("TIER1_MAX_USD") == CONFIG_DEFAULTS["TIER1_MAX_USD"]
    assert config.decimal("TIER1_MIN_CONFIDENCE") == Decimal("0.85")


def test_srd_6_23_stored_values_win_and_are_cached_for_60_seconds(dynamodb: Any) -> None:
    clock = Clock()
    config = Config(dynamodb, env="test", clock=clock)
    put(dynamodb, "TIER1_MAX_USD", {"N": "30000"})
    assert config.decimal("TIER1_MAX_USD") == Decimal("30000")

    put(dynamodb, "TIER1_MAX_USD", {"N": "40000"})
    clock.now += 59
    assert config.decimal("TIER1_MAX_USD") == Decimal("30000")
    clock.now += 2
    assert config.decimal("TIER1_MAX_USD") == Decimal("40000")


def test_unknown_keys_are_refused(dynamodb: Any) -> None:
    with pytest.raises(KeyError, match="NOT_A_KEY"):
        Config(dynamodb, env="test").get("NOT_A_KEY")


def test_kill_switch_reads_as_a_flag(dynamodb: Any) -> None:
    config = Config(dynamodb, env="test")
    assert config.kill_switch() is False
    put(dynamodb, "KILL_SWITCH", {"S": "on"})
    assert Config(dynamodb, env="test").kill_switch() is True


def test_srd_6_17_events_go_to_the_environment_bus_in_the_shared_envelope(aws: None) -> None:
    events = boto3.client("events", region_name="us-east-1")
    events.create_event_bus(Name="aera-test")
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue = sqs.create_queue(QueueName="capture")["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    events.put_rule(
        Name="all",
        EventBusName="aera-test",
        EventPattern=json.dumps({"source": [{"prefix": "aera."}]}),
    )
    events.put_targets(Rule="all", EventBusName="aera-test", Targets=[{"Id": "q", "Arn": arn}])
    envelope = EventEnvelope(
        type="SignalReceived",
        time=datetime(2026, 10, 5, 8, tzinfo=UTC),
        env="test",
        actor="system",
        data={"signalId": "s1", "channel": "EMAIL"},
    )

    publish(events, envelope, component="webhooks")

    message = json.loads(sqs.receive_message(QueueUrl=queue)["Messages"][0]["Body"])
    assert message["source"] == "aera.webhooks"
    assert message["detail-type"] == "SignalReceived"
    assert message["detail"]["id"] == envelope.id
    assert message["detail"]["data"] == {"signalId": "s1", "channel": "EMAIL"}


def test_srd_6_17_a_rejected_event_raises(aws: None) -> None:
    class Failing:
        def put_events(self, **_: Any) -> dict[str, Any]:
            return {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "InternalException"}]}

    envelope = EventEnvelope(
        type="CaseOpened", time=datetime.now(UTC), env="test", actor="system", data={}
    )
    with pytest.raises(EventPublishError, match="InternalException"):
        publish(Failing(), envelope, component="case-service")
