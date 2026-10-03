"""Realtime: live case and trace updates to open consoles (SRD 6.17, FR-AUD-03, NFR-PERF-04).

- `$connect?ticket=`: the single-use ticket from `POST /realtime/ticket` is consumed with a
  conditional delete; expired or reused tickets are refused.
- `subscribe` `{board: true}` or `{caseId}`: what this connection wants.
- `$disconnect`: forget the connection.
- Push: domain events (EventBridge rule) and new trace items (DynamoDB stream) become
  `{type, caseId, payload}` messages to the case's subscribers and, for case changes, to
  every board subscriber. Connections that are gone (410) are removed.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from services.shared.dynamo import from_item, table_name
from services.shared.trace import TraceStore

BOARD = "BOARD"
CONNECTION_SECONDS = 2 * 3600
BOARD_EVENTS = frozenset(
    {
        "CaseOpened",
        "CaseUpdated",
        "CaseClosed",
        "CaseReopened",
        "SignalQuarantined",
        "PlanRouted",
        "RunStarted",
        "RunEnded",
        "PortfolioSolved",
    }
)


@dataclass
class Realtime:
    dynamodb: Any
    management: Callable[[], Any]
    clock: Callable[[], float] = time.time
    env: str | None = None

    def __post_init__(self) -> None:
        self._table = table_name("connections", self.env)

    # WebSocket routes -------------------------------------------------------------------

    def route(self, event: dict[str, Any]) -> dict[str, Any]:
        context = event.get("requestContext") or {}
        connection = str(context.get("connectionId"))
        key = context.get("routeKey")
        if key == "$connect":
            ticket = (event.get("queryStringParameters") or {}).get("ticket") or ""
            return {"statusCode": 200 if self.connect(connection, str(ticket)) else 401}
        if key == "$disconnect":
            self.dynamodb.delete_item(TableName=self._table, Key={"PK": {"S": connection}})
            return {"statusCode": 200}
        if key == "subscribe":
            try:
                body = json.loads(event.get("body") or "{}")
            except ValueError:
                return {"statusCode": 400}
            return {"statusCode": 200 if self.subscribe(connection, body) else 400}
        return {"statusCode": 400}

    def connect(self, connection: str, ticket: str) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", ticket):
            return False
        try:
            old = self.dynamodb.delete_item(
                TableName=self._table,
                Key={"PK": {"S": f"TICKET#{ticket}"}},
                ConditionExpression="attribute_exists(PK) AND expiresAt >= :now",
                ExpressionAttributeValues={":now": {"N": str(int(self.clock()))}},
                ReturnValues="ALL_OLD",
            )
        except self.dynamodb.exceptions.ConditionalCheckFailedException:
            return False
        claims = from_item(old["Attributes"], keep_decimals=False)
        self.dynamodb.put_item(
            TableName=self._table,
            Item={
                "PK": {"S": connection},
                "userId": {"S": str(claims["userId"])},
                "ttl": {"N": str(int(self.clock()) + CONNECTION_SECONDS)},
            },
        )
        return True

    def subscribe(self, connection: str, body: dict[str, Any]) -> bool:
        if body.get("board") is True:
            target = BOARD
        elif isinstance(body.get("caseId"), str) and re.fullmatch(
            r"EXC-\d{4}-\d{4,}", body["caseId"]
        ):
            target = body["caseId"]
        else:
            return False
        self.dynamodb.update_item(
            TableName=self._table,
            Key={"PK": {"S": connection}},
            UpdateExpression="SET caseId = :c",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={":c": {"S": target}},
        )
        return True

    # Push -------------------------------------------------------------------------------

    def on_domain_event(self, event: dict[str, Any]) -> int:
        detail = event.get("detail") or {}
        kind = str(event.get("detail-type") or detail.get("type"))
        case_id = detail.get("caseId") or (detail.get("data") or {}).get("caseId")
        message = {"type": kind, "caseId": case_id, "payload": detail.get("data") or {}}
        targets = [case_id] if case_id else []
        if kind in BOARD_EVENTS:
            targets.append(BOARD)
        return self._send(targets, message)

    def on_trace_stream(self, event: dict[str, Any]) -> int:
        sent = 0
        for record in event.get("Records") or []:
            image = (record.get("dynamodb") or {}).get("NewImage")
            if record.get("eventName") != "INSERT" or not image:
                continue
            trace = TraceStore.parse(image)
            message = {
                "type": "TraceEvent",
                "caseId": trace.case_id,
                "payload": trace.model_dump(mode="json", by_alias=True),
            }
            sent += self._send([trace.case_id], message)
        return sent

    def _send(self, targets: list[str], message: dict[str, Any]) -> int:
        data = json.dumps(message, default=str).encode()
        api = self.management()
        sent = 0
        for target in dict.fromkeys(targets):
            page = self.dynamodb.query(
                TableName=self._table,
                IndexName="GSI1",
                KeyConditionExpression="caseId = :c",
                ExpressionAttributeValues={":c": {"S": target}},
            )
            for item in page.get("Items", []):
                connection = item["PK"]["S"]
                try:
                    api.post_to_connection(ConnectionId=connection, Data=data)
                    sent += 1
                except api.exceptions.GoneException:
                    self.dynamodb.delete_item(TableName=self._table, Key={"PK": {"S": connection}})
        return sent


_realtime: Realtime | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> Any:
    global _realtime
    if _realtime is None:
        import os

        import boto3

        from services.shared import runtime

        endpoint = os.environ.get("AERA_WS_MANAGEMENT_URL", "")
        _realtime = Realtime(
            dynamodb=runtime.client("dynamodb"),
            management=lambda: boto3.client("apigatewaymanagementapi", endpoint_url=endpoint),
        )
    if "routeKey" in (event.get("requestContext") or {}):
        return _realtime.route(event)
    if "Records" in event:
        return {"sent": _realtime.on_trace_stream(event)}
    return {"sent": _realtime.on_domain_event(event)}
