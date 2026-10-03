"""Outbox relay: publishes events that were written in the same transaction as the state
change they announce (routing's `OUTBOX#` items; SRD 6.17).

Triggered by the cases table stream (INSERT of `OUTBOX#…`). Publishing then marking `sent`
means a crash between the two re-publishes, never loses; consumers are idempotent on the
event's content (SRD 6.17 "consumers are idempotent").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.shared.dynamo import from_item, table_name
from services.shared.runtime import emit

COMPONENT = "routing"


@dataclass
class OutboxRelay:
    dynamodb: Any
    bus: Any
    env: str | None = None

    def relay(self, event: dict[str, Any]) -> int:
        published = 0
        for record in event.get("Records") or []:
            if record.get("eventName") not in ("INSERT", "MODIFY"):
                continue
            image = (record.get("dynamodb") or {}).get("NewImage")
            if not image or not image.get("SK", {}).get("S", "").startswith("OUTBOX#"):
                continue
            item = from_item(image, keep_decimals=False)
            if item.get("sent"):
                continue
            data = dict(item.get("data") or {})
            emit(
                self.bus,
                item["eventType"],
                data,
                component=COMPONENT,
                case_id=data.get("caseId"),
                environment=self.env,
            )
            self.dynamodb.update_item(
                TableName=table_name("cases", self.env),
                Key={"PK": image["PK"], "SK": image["SK"]},
                UpdateExpression="SET sent = :yes",
                ExpressionAttributeValues={":yes": {"BOOL": True}},
            )
            published += 1
        return published


_relay: OutboxRelay | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    global _relay
    if _relay is None:
        from services.shared import runtime

        _relay = OutboxRelay(dynamodb=runtime.client("dynamodb"), bus=runtime.client("events"))
    return {"published": _relay.relay(event)}
