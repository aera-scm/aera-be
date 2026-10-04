"""Monitor (FR-MON-01..03, BR-14, FR-MET-01; AT-12, AT-13) on the executed reference plan."""

from typing import Any

import pytest

from services.conftest import RecordingBus
from services.execution.tests.test_service import (  # noqa: F401 - pytest fixtures
    CASE,
    PO,
    T0,
    approve,
    mirror,
    run_state_machine,
    sap,
    status,
    world,
)
from services.monitor.handler import Monitor
from services.shared.models import CaseStatus
from services.shared.sap_client import SapClient

ENV = "test"


@pytest.fixture
def executed(world: dict[str, Any], dynamodb: Any) -> dict[str, Any]:  # noqa: F811
    run_state_machine(world, "part-C")
    approve(dynamodb)
    run_state_machine(world, "part-A")
    assert status(world) is CaseStatus.MONITORING
    return world


def goods_receipt(sap: SapClient, po: str, qty: int) -> None:  # noqa: F811
    sap.create(
        "API_MATERIAL_DOCUMENT_SRV",
        "A_MaterialDocumentItem",
        {
            "Material": "MAT-48219",
            "Plant": "1010",
            "StorageLocation": "101A",
            "GoodsMovementType": "101",
            "PurchaseOrder": po,
            "PurchaseOrderItem": "10",
            "QuantityInEntryUnit": str(qty),
            "EntryUnit": "PC",
        },
    )


def sto_number(sap: SapClient) -> str:  # noqa: F811
    rows = sap.query(PO, "A_PurchaseOrder", filter="PurchaseOrderType eq 'UB'")
    [row] = rows
    return str(row.data["PurchaseOrder"])


def test_at_12_missing_goods_receipt_reopens_the_case_with_rollback_offered(
    executed: dict[str, Any],
    sap: SapClient,
    dynamodb: Any,
    bus: RecordingBus,  # noqa: F811
) -> None:
    monitor = Monitor(dynamodb=dynamodb, sap=sap, bus=bus, clock=lambda: T0, env=ENV)

    result = monitor.check(CASE, "part-C")

    assert result["outcome"] == "REOPENED"
    assert status(executed) is CaseStatus.REOPENED
    [reopened] = bus.details("CaseReopened")
    assert reopened["data"]["rollbackAvailable"] is True
    assert "0 of 600" in reopened["data"]["reason"]


def test_at_13_posted_goods_receipts_close_the_case_with_metrics(
    executed: dict[str, Any],
    sap: SapClient,
    dynamodb: Any,
    bus: RecordingBus,  # noqa: F811
) -> None:
    monitor = Monitor(dynamodb=dynamodb, sap=sap, bus=bus, clock=lambda: T0, env=ENV)
    goods_receipt(sap, sto_number(sap), 600)

    assert monitor.check(CASE, "part-C")["outcome"] == "RECEIVED"  # part A not yet in
    goods_receipt(sap, "4500001234", 640)
    result = monitor.check(CASE, "part-A")

    assert result["outcome"] == "CLOSED"
    assert status(executed) is CaseStatus.CLOSED
    [closed] = bus.details("CaseClosed")
    metrics = closed["data"]["metrics"]
    assert {"minutesToPlan", "minutesToExecution", "minutesToClosure", "humanTouches"} <= set(
        metrics
    )
    assert metrics["minutesToClosure"] is not None


@pytest.mark.parametrize("part_id", ["deleted-before-reset", "part-C"])
def test_fr_mon_02_stale_or_unexecuted_part_cannot_close_case(
    world: dict[str, Any], dynamodb: Any, sap: SapClient, bus: RecordingBus, part_id: str
) -> None:
    monitor = Monitor(dynamodb=dynamodb, sap=sap, bus=bus, clock=lambda: T0, env=ENV)
    before = status(world)

    result = monitor.check(CASE, part_id)

    assert result == {"outcome": "IGNORED", "reason": "no successful execution receipts"}
    assert status(world) is before
    assert monitor.control.get(CASE, f"RECEIVED#{part_id}") is None
    assert "CaseClosed" not in bus.types()


def test_fr_mon_02_unknown_part_cannot_close_executed_case(
    executed: dict[str, Any], dynamodb: Any, sap: SapClient, bus: RecordingBus
) -> None:
    monitor = Monitor(dynamodb=dynamodb, sap=sap, bus=bus, clock=lambda: T0, env=ENV)

    assert monitor.check(CASE, "deleted-before-reset")["outcome"] == "IGNORED"
    assert status(executed) is CaseStatus.MONITORING
    assert monitor.control.get(CASE, "RECEIVED#deleted-before-reset") is None
