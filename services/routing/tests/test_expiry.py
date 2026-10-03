"""BR-23 after the deadline (ADR-0036, SRD 6.4): the case leaves AWAITING_APPROVAL. With a
backup approver it is re-planned and re-verified for the backup; without one it escalates."""

from datetime import timedelta
from typing import Any

import pytest

from services.conftest import RecordingBus
from services.routing.logic import route
from services.routing.store import ControlStore
from services.run_starter.handler import RunStarter
from services.run_starter.tests.test_run_starter import FakeAgentCore
from services.shared.case_state import can_transition
from services.shared.cases import CaseStore
from services.shared.dynamo import to_item
from services.shared.models import Case, CaseStatus
from services.verifier.tests.test_verification import NOW, limits, reference, verified

ENV = "test"
CASE = "EXC-2026-0914"
STATES = (
    CaseStatus.TRIAGED,
    CaseStatus.INVESTIGATING,
    CaseStatus.PLAN_PROPOSED,
    CaseStatus.VERIFIED,
    CaseStatus.AWAITING_APPROVAL,
)


def awaiting(dynamodb: Any, *, backup: bool = True) -> tuple[ControlStore, str]:
    """The reference case routed to Tier 2 and waiting for `primary` (backup `backup`)."""
    cases = CaseStore(dynamodb, ENV, clock=lambda: NOW)
    cases.create(
        Case(
            case_id=CASE,
            type="MRP_EXCEPTION",
            material="M1",
            plant="1010",
            status=CaseStatus.RECEIVED,
            created_at=NOW,
            updated_at=NOW,
        ),
        actor="system",
    )
    for state in STATES:
        cases.transition(CASE, state, actor="system")
    store = ControlStore(dynamodb, ENV)
    dynamodb.update_item(
        TableName=store.table,
        Key=to_item({"PK": f"CASE#{CASE}", "SK": "META"}),
        UpdateExpression="SET planVersion = :v",
        ExpressionAttributeValues=to_item({":v": 1}),
    )
    for limit in limits():
        if backup or limit.user_id == "primary":
            dynamodb.put_item(
                TableName=store.config,
                Item=to_item({"PK": f"APPR#{limit.user_id}", **limit.model_dump(by_alias=True)}),
            )
    dynamodb.put_item(
        TableName=store.config, Item=to_item({"PK": "CFG#KILL_SWITCH", "value": "off"})
    )
    value = verified()
    routed = route(
        value, now=NOW, stockout=NOW + timedelta(hours=24), plant="1010", limits=limits()
    )
    store.save(value, routed, plant="1010", now=NOW)
    [part] = [p for p in routed.parts if p.tier == 2]
    return store, part.id


def status(dynamodb: Any) -> CaseStatus:
    case = CaseStore(dynamodb, ENV).get(CASE)
    assert case is not None
    return case.status


def test_BR_23_expiry_with_a_backup_keeps_the_expired_approver_and_asks_for_reverification(
    dynamodb: Any,
) -> None:
    store, part = awaiting(dynamodb)

    store.tick(CASE, part, now=NOW + timedelta(hours=4))

    stored = store.get(CASE, f"PART#{part}")
    assert stored is not None
    assert (stored["expired"], stored["expiredApprover"]) == (True, "primary")
    outbox = store.get(CASE, f"OUTBOX#CaseReadyForRun#{part}#APPROVAL_EXPIRED")
    assert outbox is not None and outbox["data"]["requiresReverification"] is True
    assert status(dynamodb) is CaseStatus.AWAITING_APPROVAL  # the re-plan run moves it


def test_BR_23_expiry_without_a_backup_escalates_the_case(dynamodb: Any) -> None:
    store, part = awaiting(dynamodb, backup=False)

    store.tick(CASE, part, now=NOW + timedelta(hours=4))

    assert status(dynamodb) is CaseStatus.ESCALATED


def test_BR_23_an_expired_approval_is_re_planned(dynamodb: Any, bus: RecordingBus) -> None:
    store, part = awaiting(dynamodb)
    agentcore = FakeAgentCore()
    starter = RunStarter(
        dynamodb=dynamodb, agentcore=agentcore, runtime_arn=lambda: "arn", bus=bus, env=ENV
    )

    refused = starter.start(CASE, reason="APPROVAL_DEADLINE")
    store.tick(CASE, part, now=NOW + timedelta(hours=4))
    started = starter.start(CASE, reason="APPROVAL_DEADLINE")

    assert refused.run_id is None  # a live approval request is never re-planned
    assert started.run_id is not None
    assert status(dynamodb) is CaseStatus.INVESTIGATING


def test_BR_23_the_next_route_moves_to_the_backup_approver(dynamodb: Any) -> None:
    store, part = awaiting(dynamodb)
    store.tick(CASE, part, now=NOW + timedelta(hours=4))

    # The re-plan is verified and routed afresh (verification at most 60 s old).
    rerouted = route(
        verified(),
        now=NOW,
        stockout=NOW + timedelta(hours=24),
        plant="1010",
        limits=store.limits(case_id=CASE),
    )

    assert {p.user_id for p in store.limits(case_id=CASE)} == {"backup"}
    assert [p.approver for p in rerouted.parts if p.tier == 2] == ["backup"]
    assert {p.user_id for p in store.limits()} == {"primary", "backup"}


@pytest.mark.parametrize("target", [CaseStatus.INVESTIGATING, CaseStatus.ESCALATED])
def test_SRD_6_4_awaiting_approval_may_leave_after_the_deadline(target: CaseStatus) -> None:
    assert can_transition(CaseStatus.AWAITING_APPROVAL, target)


def test_BR_23_escalation_is_retried_when_the_timer_stopped_after_its_write(
    dynamodb: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review WP-8c: the part is expired durably; a crash before the case moved must not
    leave AWAITING_APPROVAL with nobody to decide."""
    store, part = awaiting(dynamodb, backup=False)

    def crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Lambda stopped")

    with monkeypatch.context() as patch:
        patch.setattr(store.cases, "transition", crash)
        with pytest.raises(RuntimeError):
            store.tick(CASE, part, now=NOW + timedelta(hours=4))
    store.tick(CASE, part, now=NOW + timedelta(hours=4, minutes=1))

    assert status(dynamodb) is CaseStatus.ESCALATED


def test_BR_23_a_second_expiry_never_returns_to_an_approver_who_missed_one(
    dynamodb: Any,
) -> None:
    store, part = awaiting(dynamodb)
    third = limits()[1].model_copy(update={"user_id": "third"})
    dynamodb.put_item(
        TableName=store.config,
        Item=to_item({"PK": "APPR#third", **third.model_dump(by_alias=True)}),
    )
    store.tick(CASE, part, now=NOW + timedelta(hours=4))  # primary missed version 1
    dynamodb.update_item(
        TableName=store.table,
        Key=to_item({"PK": f"CASE#{CASE}", "SK": "META"}),
        UpdateExpression="SET planVersion = :v",
        ExpressionAttributeValues=to_item({":v": 2}),
    )
    plan, evidence = reference()
    plan = plan.model_copy(update={"plan_version": 2})
    value = verified(plan, evidence)
    second = route(
        value,
        now=NOW,
        stockout=NOW + timedelta(hours=24),
        plant="1010",
        limits=store.limits(case_id=CASE),
    )
    store.save(value, second, plant="1010", now=NOW)
    [pending] = [p for p in second.parts if p.tier == 2]
    assert pending.approver == "backup"

    store.tick(CASE, pending.id, now=NOW + timedelta(hours=4))

    stored = store.get(CASE, f"PART#{pending.id}")
    assert stored is not None and stored["approver"] == "third"
