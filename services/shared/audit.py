"""Append-only, hash-chained audit events (DR-09, FR-AUD-01, FR-AUD-02).

Each event links to the previous event of its chain (`CASE#<id>` or `SIGNAL#<id>`) by
hash. The event and the chain head advance in one DynamoDB transaction whose condition is
the head the writer read, so concurrent writers cannot fork the chain: a writer that lost
the race re-reads the head and retries. Events are only ever put, never updated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError
from ulid import ULID

from services.shared.dynamo import from_item, table_name, to_item
from services.shared.models import AuditEvent
from services.shared.observability import get_logger

GENESIS = "0" * 64
_HEAD_SK = "HEAD"
_MAX_ATTEMPTS = 5
_logger = get_logger("audit")


class TransactionConflictError(Exception):
    """A caller-supplied condition in the audited transaction failed."""

    def __init__(self, failed: list[int]) -> None:
        self.failed = failed
        super().__init__(f"conditions failed for transaction items {failed}")


def _canonical(value: Any) -> Any:
    # Stable across a DynamoDB round trip: numbers become normalised decimal strings.
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int | float | Decimal):
        return format(Decimal(str(value)).normalize(), "f")
    return str(value)


def event_hash(event: AuditEvent | dict[str, Any]) -> str:
    data = event.model_dump(mode="json", by_alias=True) if isinstance(event, AuditEvent) else event
    ts = datetime.fromisoformat(str(data["ts"])).astimezone(UTC)
    material = {
        "eventId": data["eventId"],
        "chainKey": data["chainKey"],
        "caseId": data.get("caseId"),
        "ts": ts.isoformat(timespec="microseconds"),
        "actor": data["actor"],
        "type": data["type"],
        "payload": _canonical(data["payload"]),
        "prevHash": data["prevHash"],
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_chain(events: Sequence[AuditEvent]) -> list[str]:
    """Return the ids of events whose hash or link does not hold, in order."""
    broken = []
    previous = GENESIS
    for event in events:
        if event.prev_hash != previous or event.hash != event_hash(event):
            broken.append(event.event_id)
        previous = event.hash
    return broken


def _after(last_event_id: str | None) -> str:
    # ULIDs order the audit table's sort key; keep them strictly increasing per chain.
    candidate = ULID()
    if last_event_id and int(candidate) <= int(ULID.from_str(last_event_id)):
        candidate = ULID.from_int(int(ULID.from_str(last_event_id)) + 1)
    return str(candidate)


class AuditWriter:
    def __init__(self, client: Any, env: str | None = None) -> None:
        self._client = client
        self._audit = table_name("audit", env)
        self._heads = table_name("cases", env)

    def head(self, chain_key: str) -> tuple[str, str | None]:
        item = self._client.get_item(
            TableName=self._heads,
            Key={"PK": {"S": f"AUDIT#{chain_key}"}, "SK": {"S": _HEAD_SK}},
            ConsistentRead=True,
        ).get("Item")
        if not item:
            return GENESIS, None
        data = from_item(item)
        return str(data["lastHash"]), str(data["lastEventId"])

    def record(
        self,
        chain_key: str,
        type: str,
        *,
        actor: str,
        payload: dict[str, Any],
        case_id: str | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
        extra: Sequence[dict[str, Any]] = (),
        _assumed_head: tuple[str, str | None] | None = None,
    ) -> AuditEvent:
        head = _assumed_head
        for _ in range(_MAX_ATTEMPTS):
            previous_hash, previous_id = head or self.head(chain_key)
            head = None
            draft = {
                "eventId": _after(previous_id),
                "chainKey": chain_key,
                "caseId": case_id,
                "ts": datetime.now(UTC).isoformat(),
                "actor": actor,
                "type": type,
                "payload": payload,
                "prevHash": previous_hash,
            }
            event = AuditEvent.model_validate({**draft, "hash": event_hash(draft)})
            head_update: dict[str, Any] = {
                "Put": {
                    "TableName": self._heads,
                    "Item": to_item(
                        {
                            "PK": f"AUDIT#{chain_key}",
                            "SK": _HEAD_SK,
                            "lastHash": event.hash,
                            "lastEventId": event.event_id,
                        }
                    ),
                    "ConditionExpression": (
                        "attribute_not_exists(PK)"
                        if previous_id is None
                        else "lastHash = :previous"
                    ),
                }
            }
            if previous_id is not None:
                head_update["Put"]["ExpressionAttributeValues"] = {
                    ":previous": {"S": previous_hash}
                }
            items = [
                {
                    "Put": {
                        "TableName": self._audit,
                        "Item": to_item(
                            {
                                "PK": chain_key,
                                "SK": event.event_id,
                                **event.model_dump(mode="json", by_alias=True),
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
                head_update,
                *extra,
            ]
            try:
                self._client.transact_write_items(TransactItems=items)
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                    raise
                reasons = error.response.get("CancellationReasons", [])
                failed = [
                    index
                    for index, reason in enumerate(reasons)
                    if reason.get("Code") not in (None, "None")
                ]
                extras_failed = [index - 2 for index in failed if index >= 2]
                if extras_failed:
                    raise TransactionConflictError(extras_failed) from None
                continue  # the chain moved on; re-read the head and link again
            _logger.info(
                "audit event",
                caseId=case_id,
                runId=run_id,
                traceId=trace_id,
                auditType=type,
                eventId=event.event_id,
                chainKey=chain_key,
                actor=actor,
            )
            return event
        raise RuntimeError(f"audit chain {chain_key} is contended; gave up after {_MAX_ATTEMPTS}")

    def events(self, chain_key: str) -> list[AuditEvent]:
        items: list[dict[str, Any]] = []
        arguments: dict[str, Any] = {
            "TableName": self._audit,
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": {"S": chain_key}},
            "ConsistentRead": True,
        }
        while True:
            page = self._client.query(**arguments)
            items.extend(page["Items"])
            if "LastEvaluatedKey" not in page:
                break
            arguments["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        events = []
        for item in items:
            data = from_item(item, keep_decimals=False)
            data.pop("PK"), data.pop("SK")
            events.append(AuditEvent.model_validate(data))
        return events
