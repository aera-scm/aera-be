"""Outbox relay (SRD 6.17): transactional events reach the bus exactly once per item."""

from typing import Any

from boto3.dynamodb.types import TypeSerializer

from services.conftest import RecordingBus
from services.outbox.handler import OutboxRelay

ENV = "test"


def image(item: dict[str, Any]) -> dict[str, Any]:
    return {k: TypeSerializer().serialize(v) for k, v in item.items()}


def test_plan_approved_in_the_outbox_is_published_and_marked_sent(
    dynamodb: Any, bus: RecordingBus
) -> None:
    item = {
        "PK": "CASE#EXC-2026-0914",
        "SK": "OUTBOX#PlanApproved#part-C",
        "eventType": "PlanApproved",
        "data": {"caseId": "EXC-2026-0914", "planPartId": "part-C", "approvalKind": "AUTO"},
        "sent": False,
    }
    dynamodb.put_item(TableName="aera-test-cases", Item=image(item))
    relay = OutboxRelay(dynamodb=dynamodb, bus=bus, env=ENV)
    records = [
        {"eventName": "INSERT", "dynamodb": {"NewImage": image(item)}},
        {"eventName": "INSERT", "dynamodb": {"NewImage": image({**item, "SK": "META"})}},
        {"eventName": "REMOVE", "dynamodb": {}},
    ]

    assert relay.relay({"Records": records}) == 1
    [event] = bus.details("PlanApproved")
    assert event["caseId"] == "EXC-2026-0914" and event["data"]["planPartId"] == "part-C"
    stored = dynamodb.get_item(
        TableName="aera-test-cases",
        Key={"PK": {"S": item["PK"]}, "SK": {"S": item["SK"]}},
    )["Item"]
    assert stored["sent"] == {"BOOL": True}
    sent = {**item, "sent": True}
    assert (
        relay.relay({"Records": [{"eventName": "MODIFY", "dynamodb": {"NewImage": image(sent)}}]})
        == 0
    )
