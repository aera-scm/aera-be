"""FR-OPZ-03 (ADR-0044): a portfolio solve re-sized a case that waits for approval. Its
undecided approval is superseded and the optimizer's sizing becomes the next plan version,
or the case escalates when nothing it can run is left. An approval decided first wins."""

from datetime import timedelta
from typing import Any

import pytest

from services.routing.store import ApprovalConflict, ControlStore
from services.routing.tests.test_expiry import CASE, ENV, awaiting, status
from services.shared.audit import AuditWriter
from services.shared.cases import CaseStore
from services.shared.dynamo import to_item
from services.shared.models import Case, CaseStatus
from services.verifier.tests.test_verification import NOW, verified


def resized() -> dict[str, Any]:
    plan = verified().record.plan.model_copy(update={"plan_version": 2})
    return {
        "plan": plan.model_dump(mode="json", by_alias=True),
        "proposedAt": NOW.isoformat(),
        "resizedBy": "optimizer",
        "portfolioId": "PF-test",
        "reallocatedFor": "EXC-2026-0950",
    }


def case(dynamodb: Any) -> Case:
    found = CaseStore(dynamodb, ENV).get(CASE)
    assert found is not None
    return found


def reallocate(store: ControlStore, dynamodb: Any, plan: dict[str, Any] | None) -> str:
    return store.reallocate(
        case(dynamodb), plan, portfolio_id="PF-test", trigger="EXC-2026-0950", now=NOW
    )


def approve(store: ControlStore, version_hash: str) -> dict[str, Any]:
    return store.decide(
        CASE,
        actor="primary",
        groups=frozenset({"approver"}),
        version_hash=version_hash,
        decision="APPROVED",
        comment="Checked",
        now=NOW,
    )


def test_fr_opz_03_awaiting_case_gets_the_resized_plan_as_its_next_version(
    dynamodb: Any,
) -> None:
    store, part = awaiting(dynamodb)

    assert reallocate(store, dynamodb, resized()) == "reproposed"

    current = case(dynamodb)
    assert (current.status, current.plan_version) == (CaseStatus.INVESTIGATING, 2)
    plan = store.get(CASE, "PLAN#2")
    assert plan is not None and plan["resizedBy"] == "optimizer"
    old = store.get(CASE, f"PART#{part}")
    assert old is not None and old["superseded"] is True and old["supersededBy"] == 2
    outbox = store.get(CASE, "OUTBOX#PlanProposed#2")
    assert outbox is not None and outbox["data"] == {"caseId": CASE, "planVersion": 2}
    event = AuditWriter(dynamodb, ENV).events(f"CASE#{CASE}")[-1]
    assert event.type == "STATE_TRANSITION"
    assert event.payload["reason"] == "PORTFOLIO_REALLOCATED"
    assert (event.payload["from"], event.payload["to"]) == ("AWAITING_APPROVAL", "INVESTIGATING")


def test_fr_opz_03_br_23_a_superseded_approval_gets_no_reminder_or_expiry(
    dynamodb: Any,
) -> None:
    store, part = awaiting(dynamodb)
    reallocate(store, dynamodb, resized())

    store.tick(CASE, part, now=NOW + timedelta(hours=1, minutes=30))
    store.tick(CASE, part, now=NOW + timedelta(hours=4))

    old = store.get(CASE, f"PART#{part}")
    assert old is not None and not old["reminded"] and not old["expired"]
    assert store.get(CASE, f"OUTBOX#NotificationRequested#{part}#APPROVAL_REMINDER") is None
    assert status(dynamodb) is CaseStatus.INVESTIGATING


def test_fr_opz_03_an_approval_after_the_reallocation_is_stale(dynamodb: Any) -> None:
    store, _ = awaiting(dynamodb)
    route = store.get(CASE, "ROUTE#1")
    assert route is not None
    reallocate(store, dynamodb, resized())

    with pytest.raises(ApprovalConflict):
        approve(store, route["versionHash"])


def test_fr_opz_03_an_approval_decided_first_wins(dynamodb: Any) -> None:
    store, part = awaiting(dynamodb)
    route = store.get(CASE, "ROUTE#1")
    assert route is not None
    approve(store, route["versionHash"])

    assert reallocate(store, dynamodb, resized()) != "reproposed"
    assert case(dynamodb).plan_version == 1
    assert store.get(CASE, "PLAN#2") is None


def test_fr_opz_03_an_approval_racing_the_reallocation_wins(
    dynamodb: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approval commits between the reallocation's reads and its transaction: the part's
    revision moved, so the whole reallocation is dropped and nothing is half-applied."""
    store, part = awaiting(dynamodb)
    route = store.get(CASE, "ROUTE#1")
    assert route is not None
    record = store.audit.record

    def approve_first(*args: Any, **kwargs: Any) -> Any:
        if args[1] == "STATE_TRANSITION":
            approve(store, route["versionHash"])
        return record(*args, **kwargs)

    monkeypatch.setattr(store.audit, "record", approve_first)

    assert reallocate(store, dynamodb, resized()) == "changed concurrently"
    current = case(dynamodb)
    assert (current.status, current.plan_version) == (CaseStatus.AWAITING_APPROVAL, 1)
    assert store.get(CASE, "PLAN#2") is None
    decided = store.get(CASE, f"PART#{part}")
    assert decided is not None and decided["decision"] == "APPROVED"
    assert not decided.get("superseded")


def test_fr_opz_03_two_verifiers_reallocate_once(dynamodb: Any) -> None:
    store, _ = awaiting(dynamodb)
    seen = case(dynamodb)

    first = store.reallocate(seen, resized(), portfolio_id="PF-a", trigger="X1", now=NOW)
    second = store.reallocate(seen, resized(), portfolio_id="PF-b", trigger="X2", now=NOW)

    assert (first, second) == ("reproposed", "changed concurrently")
    plan = store.get(CASE, "PLAN#2")
    assert plan is not None and plan["portfolioId"] == "PF-test"  # the first writer's plan


def test_fr_opz_03_nothing_left_to_run_escalates(dynamodb: Any) -> None:
    store, part = awaiting(dynamodb)

    assert reallocate(store, dynamodb, None) == "escalated"

    current = case(dynamodb)
    assert (current.status, current.plan_version) == (CaseStatus.ESCALATED, 1)
    old = store.get(CASE, f"PART#{part}")
    assert old is not None and old["superseded"] is True
    outbox = store.get(CASE, "OUTBOX#PlanRouted#1#PORTFOLIO_REALLOCATED")
    assert outbox is not None and outbox["data"]["tier"] == 3


def test_fr_opz_03_an_expired_approval_is_left_to_br_23(dynamodb: Any) -> None:
    store, part = awaiting(dynamodb)
    store.tick(CASE, part, now=NOW + timedelta(hours=4))

    assert reallocate(store, dynamodb, resized()) == "approval already decided or expired"
    assert case(dynamodb).plan_version == 1


def test_fr_opz_03_a_tier_1_part_is_never_reallocated(dynamodb: Any) -> None:
    store, _ = awaiting(dynamodb)
    route = store.get(CASE, "ROUTE#1")
    assert route is not None
    route["parts"][0]["tier"] = 1
    store.client.put_item(TableName=store.table, Item=to_item(route))

    assert reallocate(store, dynamodb, resized()) == "approval already decided or expired"
