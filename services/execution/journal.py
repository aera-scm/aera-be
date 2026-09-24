"""BR-09/10 durable write claims: ambiguous attempts never issue a second write."""

import hashlib
import json
from typing import Any

from botocore.exceptions import ClientError

from services.shared.dynamo import from_item, table_name, to_item


class UncertainWrite(RuntimeError):
    """A prior request may have reached SAP; reconcile before retrying."""


def idempotency_key(case_id: str, version: int, index: int, action: str, target: str) -> str:
    fields = (case_id, str(version), str(index), action, target)
    if version < 1 or index < 0 or any(not f or "|" in f for f in fields):
        raise ValueError("invalid idempotency identity")
    return hashlib.sha256("|".join(fields).encode()).hexdigest()


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
        ).encode()
    ).hexdigest()


class Journal:
    def __init__(self, client: Any, env: str | None = None) -> None:
        self.client = client
        self.table = table_name("idempotency", env)

    def read(self, key: str) -> dict[str, Any] | None:
        item = self.client.get_item(
            TableName=self.table, Key={"PK": {"S": key}}, ConsistentRead=True
        ).get("Item")
        return from_item(item) if item else None

    def prepare(self, key: str, request: dict[str, Any], undo: dict[str, Any]) -> None:
        fingerprint = digest({"request": request, "undo": undo})
        try:
            self.client.put_item(
                TableName=self.table,
                Item=to_item(
                    {
                        "PK": key,
                        "request": request,
                        "undo": undo,
                        "fingerprint": fingerprint,
                        "status": "READY",
                    }
                ),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            old = self.read(key)
            if old is None or old["fingerprint"] != fingerprint:
                raise ValueError("idempotency key reused with different write or undo") from None

    def claim(self, key: str) -> dict[str, Any] | None:
        old = self.read(key)
        if old is None:
            raise ValueError("undo must be persisted before claiming a write")
        if old["status"] == "SUCCEEDED":
            return dict(old["result"])
        if old["status"] != "READY":
            raise UncertainWrite("write already attempted; reconciliation required")
        try:
            self.client.update_item(
                TableName=self.table,
                Key={"PK": {"S": key}},
                UpdateExpression="SET #status = :pending",
                ConditionExpression="#status = :ready",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=to_item({":pending": "PENDING", ":ready": "READY"}),
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            raise UncertainWrite("another executor claimed the write") from None
        return None

    def complete(self, key: str, result: dict[str, Any]) -> None:
        self.client.update_item(
            TableName=self.table,
            Key={"PK": {"S": key}},
            UpdateExpression="SET #status = :done, #result = :result",
            ConditionExpression="#status = :pending",
            ExpressionAttributeNames={"#status": "status", "#result": "result"},
            ExpressionAttributeValues=to_item(
                {":done": "SUCCEEDED", ":pending": "PENDING", ":result": result}
            ),
        )
