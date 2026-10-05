"""FR-OPZ-03 (ADR-0044), live finding AT-18 2026-10-05: the solve during one case's
verification re-sizes another case that already waits for approval. That case is re-proposed
with its new allocation, verified and routed again; its old approval is superseded."""

from decimal import Decimal
from typing import Any

import pytest

from services.conftest import RecordingBus
from services.optimizer.runtime import allocation_for, conforms
from services.optimizer.tests.test_runtime import (  # noqa: F401 - pytest fixtures
    OTHER,
    competing,
    solver,
)
from services.routing.store import ControlStore
from services.shared.audit import AuditWriter
from services.shared.dynamo import to_item
from services.shared.models import CaseStatus, PlanRecord
from services.tools.context import ToolContext
from services.tools.tests.test_tools import (  # noqa: F401 - pytest fixtures
    CASE,
    ENV,
    ctx,
    photo,
)
from services.verifier.service import VerifierService
from services.verifier.tests.test_service import (  # noqa: F401 - pytest fixtures
    Scheduler,
    scheduler,
    verifier,
)


@pytest.fixture
def awaiting(competing: ToolContext, verifier: VerifierService) -> ControlStore:  # noqa: F811
    """The reference plan (C 600 + A 640) waits for approval: under a USD 1,000 Tier 1 limit
    nothing is auto-approved (the AT-18 live staging). The 1030 case is still to be verified."""
    competing.dynamodb.put_item(
        TableName=f"aera-{ENV}-config",
        Item=to_item({"PK": "CFG#TIER1_MAX_USD", "value": Decimal("1000")}),
    )
    assert verifier.handle(CASE, 1)["tier"] == 2
    case = competing.cases.get(CASE)
    assert case is not None and case.status is CaseStatus.AWAITING_APPROVAL
    verifier.solver = solver
    return ControlStore(competing.dynamodb, ENV)


def verify_other(service: VerifierService) -> dict[str, Any]:
    """The 1030 case's verification, following its own resize if the solver re-sized it."""
    result = service.handle(OTHER, 1)
    if "resized" in result:
        result = service.handle(OTHER, int(result["resized"]))
    return result


def relay(store: ControlStore, service: VerifierService, version: int) -> dict[str, Any]:
    """The outbox relay publishing the reference's `PlanProposed` back to the verifier."""
    item = store.get(CASE, f"OUTBOX#PlanProposed#{version}")
    assert item is not None and item["data"] == {"caseId": CASE, "planVersion": version}
    return service.handle(CASE, version)


def test_fr_opz_03_awaiting_case_is_reproposed_verified_and_routed_again(
    awaiting: ControlStore,
    verifier: VerifierService,  # noqa: F811
    bus: RecordingBus,
) -> None:
    [old] = [p for p in (awaiting.get(CASE, "ROUTE#1") or {})["parts"] if p["tier"] == 2]

    result = verify_other(verifier)

    assert result["reproposed"] == {CASE: "reproposed"}
    case = verifier.cases.get(CASE)
    assert case is not None and (case.status, case.plan_version) == (CaseStatus.INVESTIGATING, 2)
    stored = awaiting.get(CASE, "PLAN#2") or {}
    assert stored["resizedBy"] == "optimizer" and stored["reallocatedFor"] == OTHER
    portfolio = awaiting.get(CASE, "PORTFOLIO")
    allocation = allocation_for(portfolio, CASE)
    assert allocation and conforms(PlanRecord.from_stored(stored), allocation)
    assert (awaiting.get(CASE, f"PART#{old['id']}") or {})["superseded"] is True

    routed = relay(awaiting, verifier, 2)

    assert routed["tier"] in (2, 3)
    assert awaiting.get(CASE, "ROUTE#2") is not None
    case = verifier.cases.get(CASE)
    assert case is not None and case.status in (
        CaseStatus.AWAITING_APPROVAL,
        CaseStatus.ESCALATED,
    )
    transitions = [
        (e.payload["from"], e.payload["to"], e.payload.get("reason"))
        for e in AuditWriter(verifier.dynamodb, ENV).events(f"CASE#{CASE}")
        if e.type == "STATE_TRANSITION"
    ]
    assert ("AWAITING_APPROVAL", "INVESTIGATING", "PORTFOLIO_REALLOCATED") in transitions
    assert ("INVESTIGATING", "PLAN_PROPOSED", "PORTFOLIO_REALLOCATED") in transitions
    assert relay(awaiting, verifier, 2) == {"tier": routed["tier"], "replayed": True}


def test_fr_opz_03_a_case_with_options_outside_the_model_is_left_alone(
    awaiting: ControlStore,
    verifier: VerifierService,  # noqa: F811
) -> None:
    """Without sourced freight capacity the reference's air option is not a candidate
    (ADR-0021), so the solve's allocation does not describe the reference plan."""
    verifier.dynamodb.delete_item(
        TableName=f"aera-{ENV}-config", Key={"PK": {"S": "FREIGHT#1000234#1010"}}
    )

    result = verify_other(verifier)

    assert CASE not in result.get("reproposed", {})
    case = verifier.cases.get(CASE)
    assert case is not None and (case.status, case.plan_version) == (
        CaseStatus.AWAITING_APPROVAL,
        1,
    )


def test_fr_opz_03_loop_guard_a_plan_already_resized_is_not_reproposed(
    awaiting: ControlStore,
    verifier: VerifierService,  # noqa: F811
) -> None:
    verifier.dynamodb.update_item(
        TableName=awaiting.table,
        Key=to_item({"PK": f"CASE#{CASE}", "SK": "PLAN#1"}),
        UpdateExpression="SET resizedBy = :optimizer",
        ExpressionAttributeValues=to_item({":optimizer": "optimizer"}),
    )

    result = verify_other(verifier)

    assert CASE not in result.get("reproposed", {})
    case = verifier.cases.get(CASE)
    assert case is not None and case.status is CaseStatus.AWAITING_APPROVAL
    assert case.plan_version == 1
