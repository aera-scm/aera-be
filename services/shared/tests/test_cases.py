"""Case state machine (SRD 6.4) with conditional transitions and audit (FR-AUD-01/02)."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from itertools import product
from typing import Any

import pytest

from services.shared.audit import AuditWriter, verify_chain
from services.shared.case_state import TERMINAL, TRANSITIONS, can_transition
from services.shared.cases import CaseStore, ConcurrentUpdateError, IllegalTransitionError
from services.shared.models import Case, CaseStatus
from services.shared.observability import get_logger

NOW = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)

# SRD 6.4, written out independently of the implementation.
EXPECTED = {
    "RECEIVED": {"TRIAGED"},
    "TRIAGED": {"INVESTIGATING"},
    "INVESTIGATING": {"WAITING_PLANNER", "WAITING_SUPPLIER", "PLAN_PROPOSED", "ESCALATED"},
    "WAITING_PLANNER": {"INVESTIGATING"},
    "WAITING_SUPPLIER": {"INVESTIGATING"},
    "PLAN_PROPOSED": {"VERIFIED", "INVESTIGATING"},
    "VERIFIED": {"AUTO_APPROVED", "AWAITING_APPROVAL", "ESCALATED"},
    "AWAITING_APPROVAL": {"APPROVED", "REJECTED"},
    "AUTO_APPROVED": {"EXECUTING"},
    "APPROVED": {"EXECUTING"},
    "EXECUTING": {"MONITORING", "FAILED_ROLLED_BACK", "INVESTIGATING"},
    "MONITORING": {"CLOSED", "REOPENED"},
    "REOPENED": {"INVESTIGATING", "ROLLED_BACK"},
    "ESCALATED": {"INVESTIGATING", "CLOSED"},
    "REJECTED": {"INVESTIGATING", "CLOSED"},
    "CLOSED": set(),
    "ROLLED_BACK": set(),
    "FAILED_ROLLED_BACK": set(),
}
LEGAL = [(a, b) for a, targets in EXPECTED.items() for b in sorted(targets)]
ILLEGAL = [(a, b) for a, b in product(EXPECTED, EXPECTED) if b not in EXPECTED[a]]


def new_case(case_id: str = "EXC-2026-0914", status: str = "RECEIVED") -> Case:
    return Case(
        case_id=case_id,
        type="SUPPLIER_DELAY",
        material="MAT-48219",
        plant="1010",
        po_number="4500001234",
        status=CaseStatus(status),
        created_at=NOW,
        updated_at=NOW,
    )


def test_srd_6_4_transition_table_matches_the_specification() -> None:
    assert {k.value: {v.value for v in vs} for k, vs in TRANSITIONS.items()} == EXPECTED
    assert {s.value for s in TERMINAL} == {"CLOSED", "ROLLED_BACK", "FAILED_ROLLED_BACK"}


@pytest.mark.parametrize(("source", "target"), LEGAL)
def test_srd_6_4_every_legal_transition_is_applied_and_audited(
    dynamodb: Any, source: str, target: str
) -> None:
    store = CaseStore(dynamodb, env="test")
    store.create(new_case(status=source), actor="system")

    updated = store.transition("EXC-2026-0914", CaseStatus(target), actor="system", reason="test")

    assert updated.status == CaseStatus(target)
    assert store.get("EXC-2026-0914").status == CaseStatus(target)  # type: ignore[union-attr]
    events = AuditWriter(dynamodb, env="test").events("CASE#EXC-2026-0914")
    assert [e.type for e in events] == ["CASE_CREATED", "STATE_TRANSITION"]
    assert events[-1].payload == {"from": source, "to": target, "reason": "test"}


@pytest.mark.parametrize(("source", "target"), ILLEGAL)
def test_srd_6_4_every_illegal_transition_is_rejected_without_a_write(
    dynamodb: Any, source: str, target: str
) -> None:
    assert not can_transition(CaseStatus(source), CaseStatus(target))
    store = CaseStore(dynamodb, env="test")
    store.create(new_case(status=source), actor="system")

    with pytest.raises(IllegalTransitionError):
        store.transition("EXC-2026-0914", CaseStatus(target), actor="system")

    assert store.get("EXC-2026-0914").status == CaseStatus(source)  # type: ignore[union-attr]
    assert len(AuditWriter(dynamodb, env="test").events("CASE#EXC-2026-0914")) == 1


def test_srd_6_4_a_concurrent_change_is_rejected_by_the_conditional_update(dynamodb: Any) -> None:
    store = CaseStore(dynamodb, env="test")
    store.create(new_case(status="INVESTIGATING"), actor="system")
    stale = store.get("EXC-2026-0914")
    assert stale is not None
    store.transition("EXC-2026-0914", CaseStatus.PLAN_PROPOSED, actor="agent")

    with pytest.raises(ConcurrentUpdateError):
        store.transition(
            "EXC-2026-0914", CaseStatus.WAITING_PLANNER, actor="agent", expected=stale.status
        )

    assert store.get("EXC-2026-0914").status == CaseStatus.PLAN_PROPOSED  # type: ignore[union-attr]


def test_dr_01_case_ids_come_from_an_atomic_counter(dynamodb: Any) -> None:
    store = CaseStore(dynamodb, env="test")
    store.seed_counter(2026, 913)

    assert store.next_case_id(2026) == "EXC-2026-0914"
    assert store.next_case_id(2026) == "EXC-2026-0915"
    assert store.next_case_id(2027) == "EXC-2027-0001"


def test_dr_01_creating_an_existing_case_is_refused(dynamodb: Any) -> None:
    store = CaseStore(dynamodb, env="test")
    store.create(new_case(), actor="system")

    with pytest.raises(ConcurrentUpdateError):
        store.create(new_case(), actor="system")


def test_fr_tri_01_triage_fields_are_stored_for_the_board_index(dynamodb: Any) -> None:
    store = CaseStore(dynamodb, env="test")
    store.create(new_case(), actor="system")

    store.update_triage(
        "EXC-2026-0914",
        rar_usd=Decimal("4720000"),
        stockout_at=datetime(2026, 10, 5, 14, 12, tzinfo=UTC),
        priority_score=Decimal("14160000"),
        actor="system",
    )

    case = store.get("EXC-2026-0914")
    assert case is not None and case.priority_score == Decimal("14160000")
    item = dynamodb.get_item(
        TableName="aera-test-cases", Key={"PK": {"S": "CASE#EXC-2026-0914"}, "SK": {"S": "META"}}
    )["Item"]
    assert item["priorityScore"] == {"N": "14160000"}
    assert item["status"] == {"S": "RECEIVED"}


# Audit ----------------------------------------------------------------------------------


def test_fr_aud_01_events_form_an_unbroken_hash_chain(dynamodb: Any) -> None:
    writer = AuditWriter(dynamodb, env="test")
    for index in range(5):
        writer.record("CASE#EXC-2026-0914", "NOTE", actor="system", payload={"n": index})

    events = writer.events("CASE#EXC-2026-0914")

    assert [e.payload["n"] for e in events] == [0, 1, 2, 3, 4]
    assert events[0].prev_hash == "0" * 64
    assert all(
        later.prev_hash == earlier.hash for earlier, later in zip(events, events[1:], strict=False)
    )
    assert verify_chain(events) == []


def test_fr_aud_02_tampering_with_any_event_is_detected(dynamodb: Any) -> None:
    writer = AuditWriter(dynamodb, env="test")
    for index in range(3):
        writer.record("CASE#EXC-2026-0914", "NOTE", actor="system", payload={"n": index})
    events = writer.events("CASE#EXC-2026-0914")

    tampered = [events[0], events[1].model_copy(update={"payload": {"n": 99}}), events[2]]
    removed = [events[0], events[2]]

    assert verify_chain(tampered) == [events[1].event_id]
    assert verify_chain(removed) == [events[2].event_id]


def test_fr_aud_01_interleaved_writers_keep_one_linear_chain(dynamodb: Any) -> None:
    first = AuditWriter(dynamodb, env="test")
    second = AuditWriter(dynamodb, env="test")
    for index in range(6):
        (first if index % 2 else second).record(
            "CASE#EXC-2026-0914", "NOTE", actor="system", payload={"n": index}
        )

    events = first.events("CASE#EXC-2026-0914")
    assert len(events) == 6 and verify_chain(events) == []


def test_fr_aud_01_a_stale_head_is_retried_not_forked(dynamodb: Any) -> None:
    writer = AuditWriter(dynamodb, env="test")
    writer.record("CASE#X", "NOTE", actor="system", payload={"n": 0})
    stale_head = writer.head("CASE#X")
    writer.record("CASE#X", "NOTE", actor="system", payload={"n": 1})

    writer.record("CASE#X", "NOTE", actor="system", payload={"n": 2}, _assumed_head=stale_head)

    events = writer.events("CASE#X")
    assert [e.payload["n"] for e in events] == [0, 1, 2]
    assert verify_chain(events) == []


def test_nfr_obs_01_audit_events_are_logged_as_structured_json(
    dynamodb: Any, caplog: pytest.LogCaptureFixture
) -> None:
    AuditWriter(dynamodb, env="test").record(
        "CASE#EXC-2026-0914",
        "NOTE",
        actor="system",
        payload={},
        case_id="EXC-2026-0914",
        run_id="run-1",
        trace_id="trace-1",
    )

    record = [r for r in caplog.records if r.getMessage() == "audit event"][-1]
    line = json.loads(get_logger("audit").registered_formatter.format(record))
    assert (line["caseId"], line["runId"], line["traceId"]) == ("EXC-2026-0914", "run-1", "trace-1")
    assert line["service"] == "aera-audit"
