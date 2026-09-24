"""Execution service on the reference plan against a real Mirror (SRD 6.8; AT-06, AT-07,
AT-08, AT-15, AT-17, AT-29; FR-EXE-01..08, BR-08..10, BR-17).

The plan is proposed through the real tools, routed as WP-5 routing stores it (Tier 1 STO
part C; Tier 2 air-freight part A with its PO split), then executed step by step as the
state machine would. A dedicated Mirror process keeps these writes away from other tests.
"""

from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from mirror_process import AVAILABLE, MISSING, running_mirror
from seed_config import rate_card_items

from services.case_service.handler import CaseService
from services.conftest import RecordingBus
from services.execution.service import ExecutionService, dispatch
from services.mrp_poller.handler import MrpPoller
from services.routing.logic import Part
from services.shared.cases import CaseStore
from services.shared.dynamo import from_item, to_item
from services.shared.models import (
    CaseStatus,
    ExtractedField,
    FieldStatus,
    PlanRecord,
    ProposedPlan,
    Signal,
    SignalChannel,
    SignalStatus,
    new_ulid,
)
from services.shared.sap_client import Endpoint, SapClient, Target
from services.tools import calc, case_tools
from services.tools.context import ToolContext
from services.verifier.logic import plan_hash

pytestmark = pytest.mark.skipif(not AVAILABLE, reason=MISSING)

ENV = "test"
T0 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
CASE = "EXC-2026-0914"
PO = "API_PURCHASEORDER_PROCESS_SRV"
LINE = {"PurchasingDocument": "4500001234", "PurchasingDocumentItem": "10", "ScheduleLine": "1"}


@pytest.fixture(scope="module")
def mirror() -> Iterator[str]:
    with running_mirror() as url:
        yield url


def admin(url: str, path: str, body: dict[str, Any]) -> None:
    with httpx.Client(base_url=url) as http:
        token = http.get(f"/sap/opu/odata/sap/{PO}/", headers={"x-csrf-token": "Fetch"})
        response = http.post(
            path, json=body, headers={"x-csrf-token": token.headers["x-csrf-token"]}
        )
        assert response.status_code == 200, response.text


@pytest.fixture
def sap(mirror: str) -> SapClient:
    admin(mirror, "/admin/reset", {"t0": T0.isoformat().replace("+00:00", "Z")})
    endpoint = Endpoint(base_url=mirror, target=Target.MIRROR, auth=None)
    return SapClient(read=endpoint, write=endpoint, sleep=lambda _: None)


class Scheduler:
    class exceptions:  # noqa: N801 - mirrors boto3's client.exceptions namespace
        ConflictException = type("ConflictException", (Exception,), {})

    def __init__(self) -> None:
        self.schedules: dict[str, dict[str, Any]] = {}

    def create_schedule(self, **request: Any) -> None:
        if request["Name"] in self.schedules:
            raise self.exceptions.ConflictException()
        self.schedules[request["Name"]] = request


@pytest.fixture
def world(dynamodb: Any, sap: SapClient, bus: RecordingBus) -> dict[str, Any]:
    for item in rate_card_items("2026-09-24T00:00:00Z"):
        dynamodb.put_item(TableName="aera-test-config", Item=item)
    dynamodb.put_item(
        TableName="aera-test-config",
        Item={"PK": {"S": "CFG#KILL_SWITCH"}, "key": {"S": "KILL_SWITCH"}, "value": {"S": "off"}},
    )
    cases = CaseStore(dynamodb, ENV, clock=lambda: T0)
    cases.seed_counter(2026, 913)
    MrpPoller(sap=sap, bus=bus, env=ENV).poll()
    CaseService(dynamodb=dynamodb, sap=sap, bus=bus, clock=lambda: T0, env=ENV).on_mrp(
        bus.details("MrpExceptionsPolled")[0]["data"]
    )
    quantity = add_signal(dynamodb, "QUANTITY", "640", FieldStatus.CONFIRMED, "user:planner-1")
    eta = add_signal(dynamodb, "ETA", (T0 + timedelta(days=9)).isoformat(), FieldStatus.CONFIRMED)
    cases.transition(CASE, CaseStatus.INVESTIGATING, actor="system")
    ctx = ToolContext(sap=sap, dynamodb=dynamodb, bus=bus, clock=lambda: T0, env=ENV)
    sto = calc.calc_option(ctx, CASE, "STO", {"fromPlant": "1020", "qty": 600})
    air = calc.calc_option(
        ctx,
        CASE,
        "AIR_FREIGHT",
        {
            "qtyFieldId": quantity.fields[0].field_id,
            "remainderAt": (T0 + timedelta(days=9)).isoformat(),
            "remainderSourceRef": f"signal:{eta.signal_id}/ETA",
        },
    )
    alt = calc.calc_option(ctx, CASE, "ALTERNATE_SUPPLIER", {"supplierId": "1000871", "qty": 800})
    options = [
        option("A", "Air freight the ready 640", air),
        option("B", "Alternate supplier", alt),
        option("C", "Transfer 600 from 1020", sto),
    ]
    accepted = case_tools.propose_plan(
        ctx, CASE, {"options": options, "chosen": ["C", "A"], "rationale": "C now, A behind"}
    )
    assert accepted["accepted"] is True, accepted
    cases.transition(CASE, CaseStatus.VERIFIED, actor="system")
    cases.transition(CASE, CaseStatus.AWAITING_APPROVAL, actor="system")
    plan_item = from_item(
        dynamodb.get_item(
            TableName="aera-test-cases",
            Key={"PK": {"S": f"CASE#{CASE}"}, "SK": {"S": "PLAN#1"}},
        )["Item"],
        keep_decimals=False,
    )
    plan = ProposedPlan.model_validate(plan_item["plan"])
    record = PlanRecord(plan=plan, proposed_at=T0, verified_at=T0, confidence=Decimal("0.9"))
    digest = plan_hash(plan)
    parts = (
        Part("part-C", ("C",), 1, Decimal(4100), Decimal("0.9"), False),
        Part(
            "part-A",
            ("A",),
            2,
            Decimal(38200),
            Decimal("0.9"),
            False,
            "approver@meridian-motors.example",
            None,
            T0 + timedelta(hours=4),
            T0 + timedelta(hours=2),
        ),
    )
    route = {
        "PK": f"CASE#{CASE}",
        "SK": "ROUTE#1",
        "versionHash": digest,
        "version": 1,
        "plant": "1010",
        "tier": 2,
        "parts": [asdict(p) for p in parts],
        "verifiedPlan": record.model_dump(mode="python", by_alias=True),
    }
    dynamodb.put_item(TableName="aera-test-cases", Item=to_item(route))
    for part in parts:
        dynamodb.put_item(
            TableName="aera-test-cases",
            Item=to_item(
                {
                    "PK": f"CASE#{CASE}",
                    "SK": f"PART#{part.id}",
                    **asdict(part),
                    "versionHash": digest,
                    "version": 1,
                    "plant": "1010",
                    "revision": 0,
                    "decision": None,
                    "expired": False,
                }
            ),
        )
    scheduler = Scheduler()
    service = ExecutionService(
        dynamodb=dynamodb,
        sap=sap,
        bus=bus,
        scheduler=scheduler,
        clock=lambda: T0,
        env=ENV,
        scheduler_role_arn="arn:aws:iam::000000000000:role/scheduler",
        bus_arn="arn:aws:events:us-east-1:000000000000:event-bus/aera-test",
    )
    return {"service": service, "cases": cases, "scheduler": scheduler, "digest": digest}


def add_signal(
    dynamodb: Any, name: str, value: str, status: FieldStatus, confirmed_by: str | None = None
) -> Signal:
    signal_id = new_ulid()
    record = Signal(
        signal_id=signal_id,
        channel=SignalChannel.WHATSAPP,
        sender_id="+447700900234",
        sender_verified=True,
        supplier_id="1000234",
        received_at=T0,
        raw_s3_key=f"raw/{signal_id}",
        raw_sha256="0" * 64,
        po_number="4500001234",
        material="MAT-48219",
        case_id=CASE,
        status=SignalStatus.ACCEPTED,
        fields=[
            ExtractedField(
                field_id=f"{signal_id}-01",
                signal_id=signal_id,
                name=name,  # type: ignore[arg-type]
                value=value,
                confidence=1.0,
                status=status,
                confirmed_by=confirmed_by,
            )
        ],
    )
    from services.shared.signals import SignalStore

    SignalStore(dynamodb, ENV).create(record)
    CaseStore(dynamodb, ENV).add_signal(CASE, signal_id)
    return record


def option(oid: str, name: str, draft: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": oid,
        "name": name,
        "actions": draft["actions"],
        "coverageUnits": draft["coverageUnits"],
        "arrival": draft["arrival"],
        "costUsd": draft["costUsd"],
        "costSourceRef": draft["costSourceRef"],
        "figures": draft["figures"],
        "rationale": name,
    }


def approve(dynamodb: Any) -> None:
    dynamodb.update_item(
        TableName="aera-test-cases",
        Key={"PK": {"S": f"CASE#{CASE}"}, "SK": {"S": "PART#part-A"}},
        UpdateExpression="SET decision = :approved",
        ExpressionAttributeValues={":approved": {"S": "APPROVED"}},
    )


def purchase_orders(sap: SapClient) -> list[str]:
    return sorted(r.data["PurchaseOrder"] for r in sap.query(PO, "A_PurchaseOrder", top=500))


def status(world: dict[str, Any]) -> CaseStatus:
    store: CaseStore = world["cases"]
    case = store.get(CASE)
    assert case is not None
    return case.status


def run_state_machine(world: dict[str, Any], part: str) -> list[str]:
    """The path the execution state machine takes (SRD 6.8), step by step."""
    service = world["service"]
    event = {"caseId": CASE, "planPartId": part}
    visited = ["CheckKillSwitch"]
    if dispatch(service, {**event, "step": "CheckKillSwitch"})["killed"]:
        return [*visited, "Refused"]
    result = dispatch(service, {**event, "step": "Execute"})
    visited.append("Execute")
    if result["outcome"] == "COMPLETED":
        dispatch(service, {**event, "step": "Notify"})
        dispatch(
            service,
            {
                **event,
                "step": "ScheduleGoodsReceiptCheck",
                "expectedArrival": result["expectedArrival"],
            },
        )
        dispatch(service, {**event, "step": "MarkMonitoring", "documents": result["documents"]})
        return [*visited, "Notify", "ScheduleGoodsReceiptCheck", "MarkMonitoring"]
    if result["outcome"] == "STALE":
        dispatch(service, {**event, "step": "ReturnToPlanning"})
        return [*visited, "ReturnToPlanning"]
    dispatch(service, {**event, "step": "MarkFailedRolledBack", "reason": result.get("reason")})
    return [*visited, "MarkFailedRolledBack"]


def test_at_29_urgent_sto_part_executes_while_air_freight_waits(
    world: dict[str, Any], sap: SapClient, dynamodb: Any, bus: RecordingBus
) -> None:
    before = purchase_orders(sap)

    path = run_state_machine(world, "part-C")

    assert path[-1] == "MarkMonitoring"
    [sto] = sorted(set(purchase_orders(sap)) - set(before))
    header = sap.get(PO, "A_PurchaseOrder", {"PurchaseOrder": sto})
    assert (header.data["PurchaseOrderType"], header.data["SupplyingPlant"]) == ("UB", "1020")
    reservation = dynamodb.query(
        TableName="aera-test-ledger",
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": "MAT#MAT-48219#PLANT#1020"}},
    )["Items"]
    assert any(item["SK"]["S"].startswith("RES#") for item in reservation)
    assert status(world) is CaseStatus.AWAITING_APPROVAL  # part A still waits
    assert [d["data"]["templateId"] for d in bus.details("NotificationRequested")] == [
        "SUPPLIER_PLAN_CONFIRMATION",
        "CUSTOMER_SERVICE_UPDATE",
        "PRODUCTION_PLANNING_UPDATE",
    ]
    [schedule] = world["scheduler"].schedules.values()
    assert schedule["ScheduleExpression"] == "at(2026-10-05T15:00:00)"  # T0 + 5 h + 2 h grace


def test_at_06_approved_part_writes_split_and_booking_and_reaches_monitoring(
    world: dict[str, Any], sap: SapClient, dynamodb: Any, bus: RecordingBus
) -> None:
    run_state_machine(world, "part-C")
    approve(dynamodb)

    path = run_state_machine(world, "part-A")

    assert path[-1] == "MarkMonitoring"
    assert status(world) is CaseStatus.MONITORING
    first = sap.get(PO, "A_PurchaseOrderScheduleLine", LINE)
    second = sap.get(PO, "A_PurchaseOrderScheduleLine", {**LINE, "ScheduleLine": "2"})
    assert Decimal(str(first.data["ScheduleLineOrderQuantity"])) == 640
    assert Decimal(str(second.data["ScheduleLineOrderQuantity"])) == 960
    completed = bus.details("ExecutionCompleted")[-1]["data"]
    assert any(str(d).startswith("AIR-") for d in completed["documents"])
    journal = dynamodb.scan(TableName="aera-test-idempotency")["Items"]
    assert all("undo" in item for item in journal)  # BR-10: undo saved with every write


def test_at_07_replay_creates_no_second_document(
    world: dict[str, Any], sap: SapClient, dynamodb: Any
) -> None:
    run_state_machine(world, "part-C")
    after_first = purchase_orders(sap)

    replay = dispatch(world["service"], {"caseId": CASE, "planPartId": "part-C", "step": "Execute"})

    assert replay["outcome"] == "COMPLETED"
    assert purchase_orders(sap) == after_first
    audit = dynamodb.query(
        TableName="aera-test-audit",
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": f"CASE#{CASE}"}},
    )["Items"]
    assert any(item["type"]["S"] == "EXECUTION_REPLAY" for item in audit)


def test_at_08_second_write_fails_after_retries_and_the_first_is_compensated(
    world: dict[str, Any], sap: SapClient, dynamodb: Any, mirror: str
) -> None:
    approve(dynamodb)
    original = sap.get(PO, "A_PurchaseOrderScheduleLine", LINE)
    # Part A: PATCH line 1 succeeds, POST of line 2 fails on every retry.
    admin(mirror, "/admin/fault", {"skip": 1, "count": 3, "status": 503})

    path = run_state_machine(world, "part-A")

    assert path[-1] == "MarkFailedRolledBack"
    restored = sap.get(PO, "A_PurchaseOrderScheduleLine", LINE)
    assert (
        restored.data["ScheduleLineOrderQuantity"],
        restored.data["ScheduleLineDeliveryDate"],
    ) == (
        original.data["ScheduleLineOrderQuantity"],
        original.data["ScheduleLineDeliveryDate"],
    )
    case = world["cases"].get(CASE)
    assert case is not None and (case.status, case.tier) == (CaseStatus.FAILED_ROLLED_BACK, 3)


def test_at_15_kill_switch_refuses_to_start(
    world: dict[str, Any], sap: SapClient, dynamodb: Any, bus: RecordingBus
) -> None:
    dynamodb.put_item(
        TableName="aera-test-config",
        Item={"PK": {"S": "CFG#KILL_SWITCH"}, "key": {"S": "KILL_SWITCH"}, "value": {"S": "on"}},
    )
    before = purchase_orders(sap)

    assert run_state_machine(world, "part-C") == ["CheckKillSwitch", "Refused"]
    assert purchase_orders(sap) == before
    assert bus.details("ExecutionFailed")[-1]["data"]["reason"] == "KILL_SWITCH"


def test_at_17_stale_plan_aborts_before_any_write_and_returns_to_planning(
    world: dict[str, Any], sap: SapClient, dynamodb: Any, bus: RecordingBus
) -> None:
    approve(dynamodb)
    line = sap.get(PO, "A_PurchaseOrderScheduleLine", LINE)
    sap.update(
        PO,
        "A_PurchaseOrderScheduleLine",
        LINE,
        {"ScheduleLineOrderQuantity": "1500"},
        etag=str(line.etag),
    )

    path = run_state_machine(world, "part-A")

    assert path == ["CheckKillSwitch", "Execute", "ReturnToPlanning"]
    assert status(world) is CaseStatus.INVESTIGATING
    assert bus.details("CaseReadyForRun")[-1]["data"]["reason"] == "stale plan"


def test_unapproved_part_is_never_written(world: dict[str, Any], sap: SapClient) -> None:
    before = purchase_orders(sap)
    with pytest.raises(PermissionError):
        dispatch(world["service"], {"caseId": CASE, "planPartId": "part-A", "step": "Execute"})
    assert purchase_orders(sap) == before


def test_fr_exe_08_rollback_undoes_reversible_writes_and_flags_the_booking(
    world: dict[str, Any], sap: SapClient, dynamodb: Any
) -> None:
    run_state_machine(world, "part-C")
    approve(dynamodb)
    run_state_machine(world, "part-A")
    world["cases"].transition(CASE, CaseStatus.REOPENED, actor="system")
    dynamodb.put_item(
        TableName="aera-test-cases",
        Item=to_item(
            {"PK": f"CASE#{CASE}", "SK": "ROLLBACK#1", "status": "REQUESTED", "actor": "u-1"}
        ),
    )

    result = dispatch(world["service"], {"caseId": CASE, "step": "Rollback"})
    again = dispatch(world["service"], {"caseId": CASE, "step": "Rollback"})

    assert sorted(result["rolledBack"]) == ["part-A", "part-C"]
    assert again["rolledBack"] == []
    assert status(world) is CaseStatus.ROLLED_BACK
    line = sap.get(PO, "A_PurchaseOrderScheduleLine", LINE)
    assert Decimal(str(line.data["ScheduleLineOrderQuantity"])) == 1600
    audit = dynamodb.query(
        TableName="aera-test-audit",
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": f"CASE#{CASE}"}},
    )["Items"]
    kinds = {item["type"]["S"] for item in audit}
    assert {"ACTION_IRREVERSIBLE", "ROLLED_BACK"} <= kinds


def test_an_unapproved_part_leaves_the_case_untouched(world: dict[str, Any]) -> None:
    with pytest.raises(PermissionError):
        dispatch(world["service"], {"caseId": CASE, "planPartId": "part-A", "step": "Execute"})
    assert status(world) is CaseStatus.AWAITING_APPROVAL
