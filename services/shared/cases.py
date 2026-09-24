"""Case service: case records and state transitions (DR-01, SRD 6.4, FR-AUD-01).

Every transition is a DynamoDB conditional update on the state the caller saw, committed
in the same transaction as its audit event, so an illegal or concurrent transition is
rejected and never half-applied.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from services.shared.audit import AuditWriter, TransactionConflictError
from services.shared.case_state import can_transition
from services.shared.dynamo import from_item, table_name, to_item, to_value
from services.shared.models import Case, CaseStatus

_META = "META"


class IllegalTransitionError(Exception):
    """The state machine of SRD 6.4 does not allow this transition."""


class ConcurrentUpdateError(Exception):
    """The case changed since the caller read it, or already exists."""


def _key(case_id: str) -> dict[str, Any]:
    return {"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": _META}}


class CaseStore:
    def __init__(self, client: Any, env: str | None = None) -> None:
        self._client = client
        self._table = table_name("cases", env)
        self._audit = AuditWriter(client, env)

    # Reads -----------------------------------------------------------------------------

    def get(self, case_id: str) -> Case | None:
        item = self._client.get_item(TableName=self._table, Key=_key(case_id), ConsistentRead=True)
        if "Item" not in item:
            return None
        data = from_item(item["Item"], keep_decimals=False)
        for key in ("PK", "SK", "stage"):
            data.pop(key, None)
        return Case.model_validate(data)

    # Writes ----------------------------------------------------------------------------

    def create(self, case: Case, *, actor: str) -> Case:
        record = case.model_dump(mode="python", by_alias=True, exclude={"stage"})
        put = {
            "Put": {
                "TableName": self._table,
                "Item": to_item(
                    {"PK": f"CASE#{case.case_id}", "SK": _META, **record, "stage": case.stage}
                ),
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }
        try:
            self._audit.record(
                f"CASE#{case.case_id}",
                "CASE_CREATED",
                actor=actor,
                case_id=case.case_id,
                payload={"status": case.status.value, "type": case.type},
                extra=[put],
            )
        except TransactionConflictError:
            raise ConcurrentUpdateError(f"case {case.case_id} already exists") from None
        return case

    def transition(
        self,
        case_id: str,
        target: CaseStatus,
        *,
        actor: str,
        reason: str | None = None,
        expected: CaseStatus | None = None,
        run_id: str | None = None,
    ) -> Case:
        current = self.get(case_id)
        if current is None:
            raise ConcurrentUpdateError(f"case {case_id} does not exist")
        source = expected or current.status
        if not can_transition(source, target):
            raise IllegalTransitionError(
                f"{source.value} -> {target.value} is not allowed (SRD 6.4)"
            )
        now = datetime.now(UTC)
        stage = current.model_copy(update={"status": target}).stage
        update = {
            "Update": {
                "TableName": self._table,
                "Key": _key(case_id),
                "UpdateExpression": "SET #status = :to, #stage = :stage, updatedAt = :now",
                "ConditionExpression": "#status = :from",
                "ExpressionAttributeNames": {"#status": "status", "#stage": "stage"},
                "ExpressionAttributeValues": {
                    ":to": {"S": target.value},
                    ":from": {"S": source.value},
                    ":stage": {"S": stage},
                    ":now": {"S": now.isoformat()},
                },
            }
        }
        payload: dict[str, Any] = {"from": source.value, "to": target.value}
        if reason is not None:
            payload["reason"] = reason
        try:
            self._audit.record(
                f"CASE#{case_id}",
                "STATE_TRANSITION",
                actor=actor,
                case_id=case_id,
                run_id=run_id,
                payload=payload,
                extra=[update],
            )
        except TransactionConflictError:
            raise ConcurrentUpdateError(f"case {case_id} is no longer {source.value}") from None
        return current.model_copy(update={"status": target, "updated_at": now})

    def update_triage(
        self,
        case_id: str,
        *,
        rar_usd: Decimal,
        stockout_at: datetime | None,
        priority_score: Decimal,
        actor: str,
        figures: list[dict[str, Any]] | None = None,
    ) -> None:
        values: dict[str, Any] = {
            ":rar": to_value(rar_usd),
            ":score": to_value(priority_score),
            ":now": {"S": datetime.now(UTC).isoformat()},
        }
        expression = "SET rarUsd = :rar, priorityScore = :score, updatedAt = :now"
        if stockout_at is not None:
            expression += ", stockoutAt = :stockout"
            values[":stockout"] = {"S": stockout_at.isoformat()}
        if figures is not None:
            expression += ", figures = :figures"
            values[":figures"] = to_value(figures)
        self._client.update_item(
            TableName=self._table,
            Key=_key(case_id),
            UpdateExpression=expression,
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues=values,
        )
        self._audit.record(
            f"CASE#{case_id}",
            "TRIAGE_SCORED",
            actor=actor,
            case_id=case_id,
            payload={
                "rarUsd": str(rar_usd),
                "priorityScore": str(priority_score),
                "stockoutAt": stockout_at.isoformat() if stockout_at else None,
            },
        )

    def add_signal(self, case_id: str, signal_id: str) -> None:
        self._client.update_item(
            TableName=self._table,
            Key=_key(case_id),
            UpdateExpression=(
                "SET signalIds = list_append(if_not_exists(signalIds, :empty), :id),"
                " updatedAt = :now"
            ),
            ConditionExpression="attribute_exists(PK) AND NOT contains(signalIds, :raw)",
            ExpressionAttributeValues={
                ":empty": {"L": []},
                ":id": {"L": [{"S": signal_id}]},
                ":raw": {"S": signal_id},
                ":now": {"S": datetime.now(UTC).isoformat()},
            },
        )

    # Case ids --------------------------------------------------------------------------

    def next_case_id(self, year: int) -> str:
        result = self._client.update_item(
            TableName=self._table,
            Key={"PK": {"S": f"COUNTER#CASE#{year}"}, "SK": {"S": "COUNTER"}},
            UpdateExpression="ADD seq :one",
            ExpressionAttributeValues={":one": {"N": "1"}},
            ReturnValues="UPDATED_NEW",
        )
        return f"EXC-{year}-{int(result['Attributes']['seq']['N']):04d}"

    def seed_counter(self, year: int, value: int) -> None:
        self._client.put_item(
            TableName=self._table,
            Item={
                "PK": {"S": f"COUNTER#CASE#{year}"},
                "SK": {"S": "COUNTER"},
                "seq": {"N": str(value)},
            },
        )
