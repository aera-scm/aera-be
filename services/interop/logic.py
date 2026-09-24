"""BR-21: per-client rate limits and a closed external-agent operation set."""

from __future__ import annotations

import re
import time
from typing import Any

from services.shared.dynamo import table_name

ALLOWED = frozenset(
    {"submit_exception_signal", "list_cases", "get_case_status", "get_decision_record"}
)
CLIENT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
RATE_PER_MINUTE = 30


class InteropRefused(ValueError):
    pass


def authorize(operation: str, client_id: str) -> None:
    if operation not in ALLOWED:
        raise InteropRefused("external agents cannot approve, execute or change configuration")
    if not CLIENT_ID.fullmatch(client_id):
        raise InteropRefused("authenticated client id is invalid")


def rate_limit(
    client: Any, env: str, client_id: str, *, now: float | None = None, limit: int = RATE_PER_MINUTE
) -> None:
    second = int(time.time() if now is None else now)
    minute = second // 60
    try:
        client.update_item(
            TableName=table_name("idempotency", env),
            Key={"PK": {"S": f"INTEROP_RATE#{client_id}#{minute}"}},
            UpdateExpression="SET #ttl = :expiry ADD #count :one",
            ConditionExpression="attribute_not_exists(#count) OR #count < :max",
            ExpressionAttributeNames={"#ttl": "ttl", "#count": "count"},
            ExpressionAttributeValues={
                ":expiry": {"N": str(second + 180)},
                ":one": {"N": "1"},
                ":max": {"N": str(limit)},
            },
        )
    except client.exceptions.ConditionalCheckFailedException:
        raise InteropRefused("client rate limit exceeded") from None
