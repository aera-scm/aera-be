"""Signal records (DR-02, DR-03) and their immutable raw payloads (FR-ING-09).

The raw payload and every attachment are written once to the raw bucket under the signal's
id and never overwritten; the Signal item keeps the key and the SHA-256 of the payload.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from services.shared.dynamo import from_item, table_name, to_item
from services.shared.models import Signal

_META = "META"


def _key(signal_id: str) -> dict[str, Any]:
    return {"PK": {"S": f"SIG#{signal_id}"}, "SK": {"S": _META}}


def _wire_time(value: datetime) -> str:
    # The same form pydantic writes for receivedAt, so the index sorts them together.
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def raw_prefix(channel: str, signal_id: str, received_at: datetime) -> str:
    return f"raw/{channel.lower()}/{received_at:%Y/%m/%d}/{signal_id}"


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    return cleaned.strip("._") or "file"


class RawStore:
    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self.bucket = bucket

    def put_once(self, key: str, body: bytes, content_type: str) -> str:
        """Write once and return the stored object's SHA-256. An existing key is never
        replaced (conditional put); a retry gets the digest of what is already there."""
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                IfNoneMatch="*",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "PreconditionFailed":
                raise
            body = self.get(key)
        return hashlib.sha256(body).hexdigest()

    @staticmethod
    def attachment_key(prefix: str, index: int, filename: str) -> str:
        return f"{prefix}/att/{index:02d}-{_safe_name(filename)}"

    def get(self, key: str) -> bytes:
        return bytes(self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read())


class SignalStore:
    def __init__(self, client: Any, env: str | None = None) -> None:
        self._client = client
        self._table = table_name("signals", env)

    def create(self, signal: Signal) -> None:
        self._client.put_item(
            TableName=self._table,
            Item=self._item(signal),
            ConditionExpression="attribute_not_exists(PK)",
        )

    def save(self, signal: Signal) -> None:
        self._client.put_item(
            TableName=self._table,
            Item=self._item(signal),
            ConditionExpression="attribute_exists(PK)",
        )

    def get(self, signal_id: str) -> Signal | None:
        item = self._client.get_item(
            TableName=self._table, Key=_key(signal_id), ConsistentRead=True
        ).get("Item")
        return None if item is None else self.parse(item)

    def recent_for_po(self, po_number: str, since: datetime) -> list[Signal]:
        return self._query(
            "GSI2", "poNumber = :v AND receivedAt >= :since", po_number, _wire_time(since)
        )

    def for_case(self, case_id: str) -> list[Signal]:
        return self._query("GSI1", "caseId = :v", case_id)

    def _query(
        self, index: str, condition: str, value: str, since: str | None = None
    ) -> list[Signal]:
        values = {":v": {"S": value}}
        if since is not None:
            values[":since"] = {"S": since}
        signals: list[Signal] = []
        arguments: dict[str, Any] = {
            "TableName": self._table,
            "IndexName": index,
            "KeyConditionExpression": condition,
            "ExpressionAttributeValues": values,
        }
        while True:
            page = self._client.query(**arguments)
            signals.extend(self.parse(item) for item in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return signals
            arguments["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    @staticmethod
    def _item(signal: Signal) -> dict[str, Any]:
        record = signal.model_dump(mode="json", by_alias=True)
        return to_item({"PK": f"SIG#{signal.signal_id}", "SK": _META, **record})

    @staticmethod
    def parse(item: dict[str, Any]) -> Signal:
        data = from_item(item, keep_decimals=False)
        data.pop("PK", None)
        data.pop("SK", None)
        return Signal.model_validate(data)
