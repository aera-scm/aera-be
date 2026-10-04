"""FR-MET-01: a reused case number (environment reset) never mixes in an earlier case's history."""

from datetime import UTC, datetime, timedelta
from typing import Any

from services.shared.audit import AuditWriter
from services.shared.metrics import case_metrics

ENV = "test"
CASE = "EXC-2026-0919"


def test_fr_met_01_metrics_ignore_audit_events_of_an_earlier_case_with_the_same_number(
    dynamodb: Any,
) -> None:
    audit = AuditWriter(dynamodb, ENV)
    audit.record(f"CASE#{CASE}", "UNDO_SAVED", actor="system", case_id=CASE, payload={})
    audit.record(f"CASE#{CASE}", "FIELD_CONFIRMED", actor="user:old", case_id=CASE, payload={})
    created = datetime.now(UTC) + timedelta(milliseconds=5)
    while datetime.now(UTC) <= created:
        pass
    audit.record(f"CASE#{CASE}", "UNDO_SAVED", actor="system", case_id=CASE, payload={})
    audit.record(f"CASE#{CASE}", "FIELD_CONFIRMED", actor="user:new", case_id=CASE, payload={})
    events = audit.events(f"CASE#{CASE}")

    metrics = case_metrics(dynamodb, CASE, created, datetime.now(UTC), ENV)

    assert metrics["firstExecutionStartedAt"] == events[2].ts.isoformat()
    assert metrics["humanTouches"] == 1
