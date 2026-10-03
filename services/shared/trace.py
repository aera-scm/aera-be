"""Trace events for the console's trace panel (FR-AUD-03, FR-UI-04, SRD 6.20 `aera-trace`).

Items live 30 days (TTL). The table's stream feeds the realtime pusher, so writing an event
is all a producer does to make it appear live.
"""

from __future__ import annotations

import time
from typing import Any

from services.shared.dynamo import from_item, table_name, to_item
from services.shared.models import TraceEvent

TTL_DAYS = 30


class TraceStore:
    def __init__(self, client: Any, env: str | None = None) -> None:
        self._client = client
        self._table = table_name("trace", env)

    def write(self, event: TraceEvent) -> None:
        record = event.model_dump(mode="json", by_alias=True)
        self._client.put_item(
            TableName=self._table,
            Item=to_item(
                {
                    "PK": f"CASE#{event.case_id}",
                    "SK": event.event_id,
                    **record,
                    "ttl": int(time.time()) + TTL_DAYS * 86400,
                }
            ),
        )

    def events(
        self, case_id: str, *, after: str | None = None, limit: int = 500
    ) -> list[TraceEvent]:
        condition = "PK = :pk"
        values: dict[str, Any] = {":pk": {"S": f"CASE#{case_id}"}}
        if after:
            condition += " AND SK > :after"
            values[":after"] = {"S": after}
        page = self._client.query(
            TableName=self._table,
            KeyConditionExpression=condition,
            ExpressionAttributeValues=values,
            Limit=limit,
        )
        return [self.parse(item) for item in page.get("Items", [])]

    @staticmethod
    def parse(item: dict[str, Any]) -> TraceEvent:
        data = from_item(item, keep_decimals=False)
        for key in ("PK", "SK", "ttl"):
            data.pop(key, None)
        return TraceEvent.model_validate(data)
