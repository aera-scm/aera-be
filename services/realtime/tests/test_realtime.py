"""Realtime WebSocket (SRD 6.17, FR-AUD-03): tickets, subscriptions, pushes."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from boto3.dynamodb.types import TypeSerializer

from services.realtime.handler import Realtime
from services.shared.models import TraceEvent

ENV = "test"
NOW = 1_759_650_000
TICKET = "t" * 43


class Gone(Exception):
    pass


class FakeManagement:
    class exceptions:  # noqa: N801 - mirrors boto3's client.exceptions namespace
        GoneException = Gone

    def __init__(self, gone: set[str] | None = None) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.gone = gone or set()

    def post_to_connection(self, ConnectionId: str, Data: bytes) -> None:  # noqa: N803
        if ConnectionId in self.gone:
            raise Gone()
        self.sent.append((ConnectionId, json.loads(Data)))


@pytest.fixture
def management() -> FakeManagement:
    return FakeManagement(gone={"c-gone"})


@pytest.fixture
def realtime(dynamodb: Any, management: FakeManagement) -> Realtime:
    return Realtime(dynamodb=dynamodb, management=lambda: management, clock=lambda: NOW, env=ENV)


def ticket(dynamodb: Any, value: str = TICKET, expires: int = NOW + 60) -> None:
    dynamodb.put_item(
        TableName="aera-test-connections",
        Item={
            "PK": {"S": f"TICKET#{value}"},
            "userId": {"S": "u-1"},
            "expiresAt": {"N": str(expires)},
        },
    )


def ws(route: str, connection: str, **extra: Any) -> dict[str, Any]:
    return {"requestContext": {"routeKey": route, "connectionId": connection}, **extra}


def test_a_ticket_opens_exactly_one_connection(realtime: Realtime, dynamodb: Any) -> None:
    ticket(dynamodb)
    connect = ws("$connect", "c-1", queryStringParameters={"ticket": TICKET})

    assert realtime.route(connect)["statusCode"] == 200
    assert (
        realtime.route(
            {**connect, "requestContext": {"routeKey": "$connect", "connectionId": "c-2"}}
        )["statusCode"]
        == 401
    )


def test_expired_or_malformed_tickets_are_refused(realtime: Realtime, dynamodb: Any) -> None:
    ticket(dynamodb, expires=NOW - 1)
    assert not realtime.connect("c-1", TICKET)
    assert not realtime.connect("c-1", "short")


def test_board_and_case_subscribers_receive_their_events(
    realtime: Realtime, dynamodb: Any, management: FakeManagement
) -> None:
    for connection, target in (
        ("c-board", {"board": True}),
        ("c-case", {"caseId": "EXC-2026-0914"}),
        ("c-gone", {"board": True}),
    ):
        ticket(dynamodb, value=connection.replace("-", "x") * 8)
        assert realtime.connect(connection, connection.replace("-", "x") * 8)
        assert (
            realtime.route(
                ws("subscribe", connection, body=json.dumps({"action": "subscribe", **target}))
            )["statusCode"]
            == 200
        )

    sent = realtime.on_domain_event(
        {
            "detail-type": "CaseUpdated",
            "detail": {
                "type": "CaseUpdated",
                "caseId": "EXC-2026-0914",
                "data": {"reason": "signal"},
            },
        }
    )

    assert sent == 2
    assert {c for c, _ in management.sent} == {"c-board", "c-case"}
    assert management.sent[0][1] == {
        "type": "CaseUpdated",
        "caseId": "EXC-2026-0914",
        "payload": {"reason": "signal"},
    }
    gone = dynamodb.get_item(TableName="aera-test-connections", Key={"PK": {"S": "c-gone"}})
    assert "Item" not in gone


def test_fr_aud_03_new_trace_items_are_pushed_to_the_case(
    realtime: Realtime, dynamodb: Any, management: FakeManagement
) -> None:
    ticket(dynamodb)
    realtime.connect("c-case", TICKET)
    realtime.subscribe("c-case", {"caseId": "EXC-2026-0914"})
    event = TraceEvent(
        case_id="EXC-2026-0914",
        kind="TOOL_CALL",
        title="sap_get_purchase_order",
        ts=datetime(2026, 10, 5, tzinfo=UTC),
    )
    image = {
        k: TypeSerializer().serialize(v)
        for k, v in {
            "PK": "CASE#EXC-2026-0914",
            "SK": event.event_id,
            **json.loads(event.model_dump_json(by_alias=True)),
        }.items()
    }

    sent = realtime.on_trace_stream(
        {
            "Records": [
                {"eventName": "INSERT", "dynamodb": {"NewImage": image}},
                {"eventName": "REMOVE", "dynamodb": {}},
            ]
        }
    )

    assert sent == 1
    [(connection, message)] = management.sent
    assert connection == "c-case" and message["type"] == "TraceEvent"
    assert message["payload"]["title"] == "sap_get_purchase_order"


def test_bad_subscriptions_are_refused(realtime: Realtime) -> None:
    assert realtime.route(ws("subscribe", "c-1", body="{nope"))["statusCode"] == 400
    assert (
        realtime.route(ws("subscribe", "c-1", body=json.dumps({"caseId": "DROP TABLE"})))[
            "statusCode"
        ]
        == 400
    )
    assert realtime.route(ws("$default", "c-1"))["statusCode"] == 400
    assert realtime.route(ws("$disconnect", "c-1"))["statusCode"] == 200
