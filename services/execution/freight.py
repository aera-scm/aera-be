"""Air-freight bookings (SRD 6.8 EXPEDITE_AIR, BR-17).

Booking freight is a commitment to a carrier outside SAP; the hackathon has no carrier
booking API, so the workflow records the booking here, cites its rate card entry and treats
it as irreversible: compensation never pretends to cancel it (ADR-0016).
"""

from __future__ import annotations

import hashlib
from typing import Any

from services.shared.dynamo import from_item, table_name, to_item


class FreightBookings:
    def __init__(self, client: Any, case_id: str, env: str | None = None) -> None:
        self._client = client
        self._table = table_name("cases", env)
        self._case_id = case_id

    @staticmethod
    def document(key: str) -> str:
        return f"AIR-{hashlib.sha256(key.encode()).hexdigest()[:12].upper()}"

    def book(self, key: str, booking: dict[str, Any]) -> str:
        """Idempotent on the workflow key: a replay finds the same booking."""
        document = self.document(key)
        existing = self.read(document)
        if existing is not None:
            return document
        self._client.put_item(
            TableName=self._table,
            Item=to_item({"PK": f"CASE#{self._case_id}", "SK": f"FREIGHT#{document}", **booking}),
            ConditionExpression="attribute_not_exists(PK)",
        )
        return document

    def read(self, document: str) -> dict[str, Any] | None:
        item = self._client.get_item(
            TableName=self._table,
            Key={"PK": {"S": f"CASE#{self._case_id}"}, "SK": {"S": f"FREIGHT#{document}"}},
            ConsistentRead=True,
        ).get("Item")
        return from_item(item, keep_decimals=False) if item else None
