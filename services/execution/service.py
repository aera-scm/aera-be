"""Execution service: the steps the Step Functions workflow calls (SRD 6.8, UC-09, FR-EXE).

A routed and approved plan part (`ROUTE#{version}` and `PART#{id}` from routing) becomes an
`Execution` of ordered steps: reversible actions first, irreversible ones last, each keyed by
its original plan action index (BR-09). The workflow (workflow.py) revalidates, reserves,
saves the undo plan, writes, verifies and compensates; this service adds the case side:
state transitions, events, notifications and the goods-receipt schedule.

Every step is idempotent: Step Functions may retry any of them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from services.execution.freight import FreightBookings
from services.execution.journal import Journal, UncertainWrite
from services.execution.ledger import Ledger, ReservationConflict
from services.execution.sap import PO, SapBoundary
from services.execution.workflow import (
    CompensationFailed,
    Execution,
    KillSwitch,
    StalePlan,
    Step,
    Transfer,
    Workflow,
)
from services.routing.store import ControlStore
from services.shared.audit import AuditWriter
from services.shared.case_state import can_transition
from services.shared.cases import CaseStore, ConcurrentUpdateError, IllegalTransitionError
from services.shared.config import Config
from services.shared.models import (
    Action,
    BookAirFreight,
    CaseStatus,
    ChangePoDate,
    CreateSto,
    Option,
    PlanRecord,
    SplitPoScheduleLine,
)
from services.shared.runtime import emit
from services.shared.sap_client import SapClient
from services.shared.sap_values import number
from services.shared.triage import STOCK, consumption_rate, odata_quote

COMPONENT = "execution"
IRREVERSIBLE = {"BOOK_AIR_FREIGHT", "CREATE_PO_ALTERNATE"}
NOTIFICATIONS = (
    ("supplier", "SUPPLIER_PLAN_CONFIRMATION"),
    ("customer_service", "CUSTOMER_SERVICE_UPDATE"),
    ("production_planning", "PRODUCTION_PLANNING_UPDATE"),
)


def target(action: Action) -> str:
    """The SAP object an action writes (part of the BR-09 key)."""
    if isinstance(action, CreateSto):
        return f"STO#{action.from_plant}#{action.to_plant}#{action.material}"
    if isinstance(action, ChangePoDate | SplitPoScheduleLine):
        return f"PO#{action.po_number}#{action.po_item}#{action.schedule_line}"
    if isinstance(action, BookAirFreight):
        return f"AIR#{action.po_number}#{action.po_item}"
    return f"ALT#{action.supplier_id}#{action.material}#{action.plant}"


def arrival_of(action: Action, option: Option) -> datetime:
    if action.type == "BOOK_AIR_FREIGHT":
        return action.arrival
    return option.arrival


@dataclass
class ExecutionService:
    dynamodb: Any
    sap: SapClient
    bus: Any
    scheduler: Any = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    env: str | None = None
    scheduler_role_arn: str = ""
    bus_arn: str = ""

    def __post_init__(self) -> None:
        self.control = ControlStore(self.dynamodb, self.env)
        self.cases = CaseStore(self.dynamodb, self.env, clock=self.clock)
        self.config = Config(self.dynamodb, self.env)
        self.audit = AuditWriter(self.dynamodb, self.env)
        self.journal = Journal(self.dynamodb, self.env)
        self.ledger = Ledger(self.dynamodb, self.env)

    # Loading ----------------------------------------------------------------------------

    def load(self, case_id: str, part_id: str) -> tuple[Execution, dict[str, Any], PlanRecord]:
        part = self.control.get(case_id, f"PART#{part_id}")
        if part is None:
            raise PermissionError("unknown plan part")
        route = self.control.get(case_id, f"ROUTE#{part['version']}")
        if route is None or route["versionHash"] != part["versionHash"]:
            raise PermissionError("route missing or changed")
        record = PlanRecord.model_validate(route["verifiedPlan"])
        steps: list[Step] = []
        transfers: list[Transfer] = []
        index = 0
        for option in record.plan.options:
            for action in option.actions:
                if option.id in part["options"]:
                    refs = (
                        option.cost_source_ref,
                        *(f.source_ref for f in option.figures),
                    )
                    steps.append(Step(index, action, target(action), tuple(dict.fromkeys(refs))))
                    if action.type == "CREATE_STO":
                        transfers.append(
                            self._transfer(action.material, action.from_plant, action.qty)
                        )
                index += 1
        # Reversible writes first: a failure then never strands an irreversible commitment.
        steps.sort(key=lambda s: (s.action.type in IRREVERSIBLE, s.index))
        execution = Execution(
            case_id=case_id,
            version=int(part["version"]),
            part_id=part_id,
            version_hash=str(part["versionHash"]),
            steps=tuple(steps),
            transfers=tuple(transfers),
        )
        return execution, part, record

    def _transfer(self, material: str, plant: str, qty: Decimal) -> Transfer:
        """Stock free above the donor's minimum cover now (BR-07, BR-08)."""
        where = f"Material eq {odata_quote(material)} and Plant eq {odata_quote(plant)}"
        rows = self.sap.query(STOCK, "A_MatlStkInAcctMod", filter=where)
        on_hand = sum(
            (number(r.data.get("MatlWrhsStkQtyInMatlBaseUnit")) for r in rows), Decimal(0)
        )
        per_hour, _ = consumption_rate(self.sap, material, plant, self.clock())
        cover = (per_hour or Decimal(0)) * 24 * self.config.decimal("DONOR_MIN_COVER_DAYS")
        ref = rows[0].field_ref("MatlWrhsStkQtyInMatlBaseUnit") if rows else f"SAP:{STOCK}"
        return Transfer(
            material=material,
            plant=plant,
            quantity=Decimal(qty),
            available=max(on_hand - cover, Decimal(0)),
            source_ref=ref,
        )

    # Boundary ---------------------------------------------------------------------------

    def _authorized(self, execution: Execution) -> bool:
        part = self.control.get(execution.case_id, f"PART#{execution.part_id}")
        meta = self.control.get(execution.case_id, "META")
        if part is None or meta is None or part["versionHash"] != execution.version_hash:
            return False
        if int(meta.get("planVersion", 0)) != execution.version:
            return False
        if part["tier"] == 1:
            return True
        return bool(part.get("decision") == "APPROVED" and not part.get("expired"))

    def _rollback_authorized(self, execution: Execution) -> bool:
        request = self.control.get(execution.case_id, f"ROLLBACK#{execution.version}")
        return bool(request and request.get("status") == "REQUESTED")

    def _revalidate(self, execution: Execution) -> bool:
        """FR-EXE-02: dependent SAP values still as verified (stock, PO schedule lines)."""
        for transfer in execution.transfers:
            fresh = self._transfer(transfer.material, transfer.plant, transfer.quantity)
            if fresh.available < transfer.quantity:
                return False
        for step in execution.steps:
            action = step.action
            if action.type == "SPLIT_PO_SCHEDULE_LINE" and step.substep == 0:
                line = self.sap.get(
                    PO,
                    "A_PurchaseOrderScheduleLine",
                    {
                        "PurchasingDocument": action.po_number,
                        "PurchasingDocumentItem": action.po_item,
                        "ScheduleLine": action.schedule_line,
                    },
                )
                wanted = sum((p.qty for p in action.parts), Decimal(0))
                if number(line.data.get("ScheduleLineOrderQuantity")) != wanted:
                    return False
        return True

    def _audit(self, case_id: str) -> Callable[[str, dict[str, Any]], None]:
        def write(kind: str, data: dict[str, Any]) -> None:
            self.audit.record(
                f"CASE#{case_id}",
                kind,
                actor="system",
                case_id=case_id,
                payload=json.loads(json.dumps(data, default=str)),
            )

        return write

    def workflow(self, execution: Execution, record: PlanRecord) -> Workflow:
        case = self.cases.get(execution.case_id)
        po_number = case.po_number if case else None
        header_keys = (
            "CompanyCode",
            "PurchasingOrganization",
            "PurchasingGroup",
            "DocumentCurrency",
        )
        header = self.sap.get(PO, "A_PurchaseOrder", {"PurchaseOrder": str(po_number)})
        boundary = SapBoundary(
            self.sap,
            authorize=self._authorized,
            authorize_rollback=self._rollback_authorized,
            kill_switch=self.config.kill_switch,
            revalidate=self._revalidate,
            audit=self._audit(execution.case_id),
            sto_header={key: str(header.data[key]) for key in header_keys},
            bookings=FreightBookings(self.dynamodb, execution.case_id, self.env),
            key_of=lambda step: Workflow.key(execution, step),
        )
        return Workflow(self.journal, self.ledger, boundary)

    # Steps ------------------------------------------------------------------------------

    def check_kill_switch(self, case_id: str, part_id: str) -> dict[str, Any]:
        killed = self.config.kill_switch()
        if killed:
            self._emit("ExecutionFailed", case_id, {"planPartId": part_id, "reason": "KILL_SWITCH"})
            self._audit(case_id)("EXECUTION_REFUSED", {"partId": part_id, "reason": "KILL_SWITCH"})
        return {"killed": killed}

    def execute(self, case_id: str, part_id: str) -> dict[str, Any]:
        execution, part, record = self.load(case_id, part_id)
        if not self._authorized(execution):
            # Nothing happens to the case: an unapproved part is not a failed execution.
            raise PermissionError("persisted approval for exact plan part required")
        self._enter_execution(case_id, part)
        self._emit("ExecutionStarted", case_id, {"planPartId": part_id})
        try:
            results = self.workflow(execution, record).run(execution)
        except StalePlan:
            return {"outcome": "STALE"}
        except KillSwitch:
            return {"outcome": "HALTED"}
        except ReservationConflict:
            return {"outcome": "FAILED", "reason": "RESERVATION_CONFLICT"}
        except PermissionError:
            raise
        except (CompensationFailed, UncertainWrite) as error:
            return {"outcome": "FAILED", "reason": type(error).__name__, "manual": True}
        except Exception as error:  # noqa: BLE001 - compensated inside the workflow
            return {"outcome": "FAILED", "reason": type(error).__name__}
        arrivals = [
            arrival_of(step.action, next(o for o in record.plan.options if o.id in part["options"]))
            for step in execution.steps
        ]
        return {
            "outcome": "COMPLETED",
            "documents": [r.get("document") for r in results],
            "expectedArrival": max(arrivals).isoformat(),
        }

    def _enter_execution(self, case_id: str, part: dict[str, Any]) -> None:
        case = self.cases.get(case_id)
        if case is None:
            raise PermissionError("case not found")
        path = {
            CaseStatus.VERIFIED: [CaseStatus.AUTO_APPROVED, CaseStatus.EXECUTING],
            CaseStatus.AWAITING_APPROVAL: [CaseStatus.APPROVED, CaseStatus.EXECUTING],
            CaseStatus.AUTO_APPROVED: [CaseStatus.EXECUTING],
            CaseStatus.APPROVED: [CaseStatus.EXECUTING],
        }.get(case.status, [])
        if case.status is CaseStatus.AWAITING_APPROVAL and part["tier"] == 1:
            path = []  # BR-22: the urgent part runs while the rest still awaits approval
        for status in path:
            self._move(case_id, status, "execution")

    def _move(self, case_id: str, status: CaseStatus, reason: str) -> bool:
        case = self.cases.get(case_id)
        if case is None or case.status is status or not can_transition(case.status, status):
            return False
        try:
            self.cases.transition(case_id, status, actor="system", reason=reason)
        except (IllegalTransitionError, ConcurrentUpdateError):
            return False
        return True

    def notify(
        self, case_id: str, part_id: str, documents: list[Any] | None = None
    ) -> dict[str, Any]:
        """FR-COM-01: supplier, customer service and production planning; the notifier
        (WP-7) resolves recipients from SAP master data only (BR-03)."""
        for role, template in NOTIFICATIONS:
            self._emit(
                "NotificationRequested",
                case_id,
                {
                    "recipientRole": role,
                    "templateId": template,
                    "planPartId": part_id,
                    "documents": list(documents or []),
                },
            )
        return {"notified": len(NOTIFICATIONS)}

    def schedule_goods_receipt_check(
        self, case_id: str, part_id: str, expected_arrival: str
    ) -> dict[str, Any]:
        """FR-MON-01: one-shot schedule at expected arrival + grace (REOPEN_GRACE_HOURS)."""
        grace = timedelta(hours=float(self.config.decimal("REOPEN_GRACE_HOURS")))
        arrival = datetime.fromisoformat(expected_arrival)
        due = arrival + grace
        case = self.cases.get(case_id)
        if case is not None and case.stockout_at is not None:
            # BR-14: reopen no later than 1 h before the projected stock-out.
            due = max(arrival, min(due, case.stockout_at - timedelta(hours=1)))
        name = f"aera-{self.env or 'dev'}-gr-{case_id}-{part_id[:12]}"
        detail = {"caseId": case_id, "planPartId": part_id, "expected": expected_arrival}
        if self.scheduler is not None:
            try:
                self.scheduler.create_schedule(
                    Name=name,
                    ScheduleExpression=f"at({due.astimezone(UTC):%Y-%m-%dT%H:%M:%S})",
                    FlexibleTimeWindow={"Mode": "OFF"},
                    ActionAfterCompletion="DELETE",
                    Target={
                        "Arn": self.bus_arn,
                        "RoleArn": self.scheduler_role_arn,
                        "EventBridgeParameters": {
                            "DetailType": "GoodsReceiptDue",
                            "Source": "aera.scheduler",
                        },
                        "Input": json.dumps(
                            {
                                "type": "GoodsReceiptDue",
                                "env": self.env,
                                "caseId": case_id,
                                "actor": "system",
                                "data": detail,
                            }
                        ),
                    },
                )
            except self.scheduler.exceptions.ConflictException:
                pass  # a retried step: the schedule already exists
        self._audit(case_id)("GOODS_RECEIPT_CHECK_SCHEDULED", {**detail, "due": due.isoformat()})
        return {"due": due.isoformat(), "schedule": name}

    def mark_monitoring(self, case_id: str, part_id: str, documents: list[Any]) -> dict[str, Any]:
        moved = self._move(case_id, CaseStatus.MONITORING, "execution completed")
        self._emit("ExecutionCompleted", case_id, {"planPartId": part_id, "documents": documents})
        return {"monitoring": moved}

    def return_to_planning(self, case_id: str, part_id: str) -> dict[str, Any]:
        """FR-EXE-02 stale plan: back to stage 4 with a new run."""
        moved = self._move(case_id, CaseStatus.INVESTIGATING, "stale plan")
        self._emit("ExecutionFailed", case_id, {"planPartId": part_id, "reason": "STALE_PLAN"})
        if moved:
            self._emit("CaseReadyForRun", case_id, {"reason": "stale plan"})
        return {"replanning": moved}

    def mark_failed(self, case_id: str, part_id: str, reason: str) -> dict[str, Any]:
        """FR-EXE-07: compensated (or held for manual reconciliation), then escalated."""
        self._move(case_id, CaseStatus.FAILED_ROLLED_BACK, reason)
        self.dynamodb.update_item(
            TableName=self.control.table,
            Key={"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": "META"}},
            UpdateExpression="SET tier = :three",
            ExpressionAttributeValues={":three": {"N": "3"}},
        )
        self._emit("ExecutionFailed", case_id, {"planPartId": part_id, "reason": reason})
        self._emit("CaseUpdated", case_id, {"reason": "escalated", "detail": reason})
        return {"escalated": True}

    def rollback(self, case_id: str) -> dict[str, Any]:
        """FR-EXE-08: undo every executed part of the current plan version, idempotently."""
        meta = self.control.get(case_id, "META")
        if meta is None:
            raise PermissionError("case not found")
        route = self.control.get(case_id, f"ROUTE#{meta['planVersion']}")
        if route is None:
            raise PermissionError("no routed plan")
        request = self.control.get(case_id, f"ROLLBACK#{meta['planVersion']}")
        if request is not None and request.get("status") == "DONE":
            return {"rolledBack": []}  # a retried or repeated request: already done
        undone = []
        for part in route["parts"]:
            execution, _, record = self.load(case_id, part["id"])
            key = f"EXEC#{execution.case_id}#{execution.version}#{execution.part_id}"
            state = self.journal.read(key)
            if state is None or state["status"] != "SUCCEEDED":
                continue
            self.workflow(execution, record).rollback(execution)
            undone.append(part["id"])
        self._move(case_id, CaseStatus.ROLLED_BACK, "rollback")
        self.dynamodb.update_item(
            TableName=self.control.table,
            Key={"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": f"ROLLBACK#{meta['planVersion']}"}},
            UpdateExpression="SET #s = :done",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":done": {"S": "DONE"}},
        )
        self._emit("CaseUpdated", case_id, {"reason": "rolled back", "planPartIds": undone})
        return {"rolledBack": undone}

    def _emit(self, event_type: Any, case_id: str, data: dict[str, Any]) -> None:
        emit(
            self.bus,
            event_type,
            {"caseId": case_id, **data},
            component=COMPONENT,
            case_id=case_id,
            environment=self.env,
        )


_service: ExecutionService | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """One task of the execution state machine: `{"step": ..., "caseId": ..., ...}`."""
    global _service
    if _service is None:
        import os

        from services.shared import runtime

        _service = ExecutionService(
            dynamodb=runtime.client("dynamodb"),
            sap=runtime.sap_client(),
            bus=runtime.client("events"),
            scheduler=runtime.client("scheduler"),
            scheduler_role_arn=os.environ.get("AERA_SCHEDULER_ROLE_ARN", ""),
            bus_arn=os.environ.get("AERA_BUS_ARN", ""),
        )
    return dispatch(_service, event)


def dispatch(service: ExecutionService, event: dict[str, Any]) -> dict[str, Any]:
    step = event["step"]
    case_id = str(event["caseId"])
    part_id = str(event.get("planPartId") or "")
    if step == "CheckKillSwitch":
        return service.check_kill_switch(case_id, part_id)
    if step == "Execute":
        return service.execute(case_id, part_id)
    if step == "Notify":
        return service.notify(case_id, part_id, list(event.get("documents") or []))
    if step == "ScheduleGoodsReceiptCheck":
        return service.schedule_goods_receipt_check(case_id, part_id, str(event["expectedArrival"]))
    if step == "MarkMonitoring":
        return service.mark_monitoring(case_id, part_id, list(event.get("documents") or []))
    if step == "ReturnToPlanning":
        return service.return_to_planning(case_id, part_id)
    if step == "MarkFailedRolledBack":
        return service.mark_failed(case_id, part_id, str(event.get("reason") or "FAILED"))
    if step == "Rollback":
        return service.rollback(case_id)
    raise ValueError(f"unknown step {step}")
