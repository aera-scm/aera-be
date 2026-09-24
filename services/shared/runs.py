"""Agent run bookkeeping (SRD 6.17, 6.18, NFR-REL-04).

One active run per case: a conditional write on `activeRunId`. Every run leaves a
`RUN#{runId}` item with its end reason, limits used and a summary, which the next run of the
same case is given so no context is lost between runs.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from services.shared.dynamo import from_item, table_name, to_item

END_REASONS = frozenset(
    {
        "PLAN",
        "ESCALATE",
        "WAITING_PLANNER",
        "WAITING_SUPPLIER",
        "LIMIT_ITERATIONS",
        "LIMIT_TOKENS",
        "LIMIT_TIME",
        "LIMIT_ERROR",
    }
)


def _meta(case_id: str) -> dict[str, Any]:
    return {"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": "META"}}


class RunStore:
    def __init__(
        self,
        client: Any,
        env: str | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._table = table_name("cases", env)
        self._clock = clock

    def claim(self, case_id: str, run_id: str) -> bool:
        try:
            self._client.update_item(
                TableName=self._table,
                Key=_meta(case_id),
                UpdateExpression="SET activeRunId = :run, activeRunAt = :now",
                ConditionExpression="attribute_exists(PK) AND attribute_not_exists(activeRunId)",
                ExpressionAttributeValues={
                    ":run": {"S": run_id},
                    ":now": {"S": self._clock().isoformat()},
                },
            )
        except self._client.exceptions.ConditionalCheckFailedException:
            return False
        return True

    def release(
        self,
        case_id: str,
        run_id: str,
        *,
        end_reason: str,
        summary: str,
        usage: dict[str, Any] | None = None,
    ) -> None:
        if end_reason not in END_REASONS:
            raise ValueError(f"unknown end reason {end_reason}")
        self._client.put_item(
            TableName=self._table,
            Item=to_item(
                {
                    "PK": f"CASE#{case_id}",
                    "SK": f"RUN#{run_id}",
                    "runId": run_id,
                    "endReason": end_reason,
                    "summary": summary[:4000],
                    "usage": usage or {},
                    "endedAt": self._clock().isoformat(),
                }
            ),
        )
        try:
            self._client.update_item(
                TableName=self._table,
                Key=_meta(case_id),
                UpdateExpression="REMOVE activeRunId, activeRunAt",
                ConditionExpression="activeRunId = :run",
                ExpressionAttributeValues={":run": {"S": run_id}},
            )
        except self._client.exceptions.ConditionalCheckFailedException:
            pass  # already released, or another run holds the case now

    def history(self, case_id: str) -> list[dict[str, Any]]:
        page = self._client.query(
            TableName=self._table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :run)",
            ExpressionAttributeValues={
                ":pk": {"S": f"CASE#{case_id}"},
                ":run": {"S": "RUN#"},
            },
        )
        runs = [from_item(item, keep_decimals=False) for item in page.get("Items", [])]
        return sorted(runs, key=lambda r: str(r.get("endedAt", "")))

    def consecutive_failures(self, case_id: str) -> int:
        count = 0
        for run in reversed(self.history(case_id)):
            if run.get("endReason") != "LIMIT_ERROR":
                break
            count += 1
        return count
