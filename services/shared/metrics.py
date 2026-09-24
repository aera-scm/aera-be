"""Per-case metrics (FR-MET-01): time from first signal to plan, approval, execution and
closure; tokens used by agent runs; human touches. Derived from the records every
component already keeps (signals, plan, approvals, audit chain, run summaries)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from services.shared.audit import AuditWriter
from services.shared.dynamo import from_item, table_name
from services.shared.runs import RunStore
from services.shared.signals import SignalStore


def _minutes(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() / 60, 1)


def case_metrics(
    dynamodb: Any, case_id: str, created_at: datetime, closed_at: datetime, env: str | None
) -> dict[str, Any]:
    signals = SignalStore(dynamodb, env).for_case(case_id)
    first = min([s.received_at for s in signals] + [created_at])
    events = AuditWriter(dynamodb, env).events(f"CASE#{case_id}")

    def first_of(*kinds: str) -> datetime | None:
        moments = [e.ts for e in events if e.type in kinds]
        return min(moments) if moments else None

    def last_of(*kinds: str) -> datetime | None:
        moments = [e.ts for e in events if e.type in kinds]
        return max(moments) if moments else None

    plan_item = dynamodb.get_item(
        TableName=table_name("cases", env),
        Key={"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": "PLAN#1"}},
    ).get("Item")
    planned = datetime.fromisoformat(str(from_item(plan_item)["proposedAt"])) if plan_item else None
    approved = last_of("APPROVAL_DECIDED", "PLAN_ROUTED")
    executed = last_of("EXECUTION_COMPLETED")
    tokens = 0
    for run in RunStore(dynamodb, env).history(case_id):
        usage = run.get("usage") or {}
        tokens += int(usage.get("totalTokens", 0) or 0)
    return {
        "firstSignalAt": first.isoformat(),
        "minutesToPlan": _minutes(first, planned),
        "minutesToApproval": _minutes(first, approved),
        "minutesToExecution": _minutes(first, executed),
        "minutesToClosure": _minutes(first, closed_at),
        "firstExecutionStartedAt": (
            moment.isoformat() if (moment := first_of("UNDO_SAVED")) else None
        ),
        "agentTokens": tokens,
        "humanTouches": sum(1 for e in events if e.actor.startswith("user:")),
    }
