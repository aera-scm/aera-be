"""Case service: open or update cases, score them, hand them to the agent
(SRD 6.25.1 step 4, FR-ING-01, FR-ING-08, FR-TRI-01, BR-13, BR-15, UC-01, UC-02).

- `MrpExceptionsPolled`: one open case per material and plant (FR-ING-01); the poll's
  counts are kept for the board header (FR-UI-01).
- `SignalExtracted`: signals about the same PO and material within 72 h of the case's last
  activity join that case (BR-15); otherwise a new case opens. Quarantined signals never
  arrive here.

New cases are triaged at once from SAP (services/shared/triage.py), move RECEIVED ->
TRIAGED, and are announced with `CaseOpened` and `CaseReadyForRun`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from services.rules.br_15 import WINDOW, same_case
from services.shared.case_state import TERMINAL
from services.shared.cases import CaseStore
from services.shared.dynamo import table_name, to_item
from services.shared.models import Case, CaseStatus, CaseType, Signal, SignalChannel
from services.shared.runtime import emit
from services.shared.sap_client import SapClient, SapNotFoundError
from services.shared.sap_values import results
from services.shared.signals import SignalStore
from services.shared.triage import Triage, assess

COMPONENT = "case-service"
PO_SERVICE = "API_PURCHASEORDER_PROCESS_SRV"


@dataclass(frozen=True)
class Subject:
    material: str
    plant: str
    po_number: str | None = None
    po_item: str | None = None
    description: str | None = None


@dataclass
class CaseService:
    dynamodb: Any
    sap: SapClient
    bus: Any
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    env: str | None = None

    def __post_init__(self) -> None:
        self.cases = CaseStore(self.dynamodb, self.env, clock=self.clock)
        self.signals = SignalStore(self.dynamodb, self.env)
        self._table = table_name("cases", self.env)

    # MRP --------------------------------------------------------------------------------

    def on_mrp(self, data: dict[str, Any]) -> list[str]:
        opened: list[str] = []
        for message in data.get("messages") or []:
            subject = self._subject(
                str(message["material"]),
                str(message["plant"]),
                po_number=_po(message.get("element")),
            )
            existing = self._open_case(_material_key(subject.material, subject.plant))
            if existing is not None:
                self._touch(existing, reason="mrp")
                continue
            case = self._open(subject, "MRP_EXCEPTION", days_late=_days_late(message))
            opened.append(case.case_id)
        self._put(
            {
                "PK": "BOARD",
                "SK": "MRP",
                "total": int(data.get("total", 0)),
                "actionable": int(data.get("actionableCount", len(data.get("messages") or []))),
                "suppressed": int(data.get("suppressedCount", 0)),
                "polledAt": self.clock().isoformat(),
            }
        )
        return opened

    # Signals ----------------------------------------------------------------------------

    def on_signal(self, signal_id: str) -> str | None:
        signal = self.signals.get(signal_id)
        if signal is None or signal.case_id is not None or not signal.sender_verified:
            return None if signal is None else signal.case_id
        if not signal.po_number:
            return None  # nothing to key a case on; the console lists it as unassigned
        subject = self._subject_for_po(signal.po_number, signal.material)
        if subject is None:
            return None
        case = self._case_for_signal(signal, subject)
        if case is None:
            case = self._open(subject, _signal_case_type(signal), first_signal=signal)
        else:
            self._attach(case, signal)
            self._touch(case, reason="signal", signal_id=signal_id)
        return case.case_id

    def _case_for_signal(self, signal: Signal, subject: Subject) -> Case | None:
        key = _po_key(signal.po_number or "", subject.material)
        case = self._open_case(key) or self._open_case(
            _material_key(subject.material, subject.plant)
        )
        if case is None:
            return None
        if case.po_number and case.po_number != signal.po_number:
            return None
        # BR-15: within 72 h of the case's latest activity.
        if not same_case(
            signal.po_number,
            subject.material,
            signal.received_at,
            signal.po_number,
            subject.material,
            case.updated_at,
            WINDOW,
        ):
            return None
        return case

    # Opening, updating ------------------------------------------------------------------

    def _open(
        self,
        subject: Subject,
        case_type: CaseType,
        *,
        first_signal: Signal | None = None,
        days_late: Decimal | None = None,
    ) -> Case:
        now = self.clock()
        case = Case(
            case_id=self.cases.next_case_id(now.year),
            type=case_type,
            material=subject.material,
            material_description=subject.description,
            plant=subject.plant,
            po_number=subject.po_number,
            po_item=subject.po_item,
            status=CaseStatus.RECEIVED,
            days_late=days_late,
            created_at=now,
            updated_at=now,
        )
        self.cases.create(case, actor="system")
        self._put_key(_material_key(case.material, case.plant), case.case_id)
        if case.po_number:
            self._put_key(_po_key(case.po_number, case.material), case.case_id)
        if first_signal is not None:
            self._attach(case, first_signal)
        triage = self._triage(case)
        self.cases.transition(
            case.case_id, CaseStatus.TRIAGED, actor="system", expected=CaseStatus.RECEIVED
        )
        emit(
            self.bus,
            "CaseOpened",
            self._summary(case, triage, CaseStatus.TRIAGED),
            component=COMPONENT,
            case_id=case.case_id,
            environment=self.env,
        )
        emit(
            self.bus,
            "CaseReadyForRun",
            {"caseId": case.case_id, "reason": "opened"},
            component=COMPONENT,
            case_id=case.case_id,
            environment=self.env,
        )
        return case

    def _touch(self, case: Case, *, reason: str, signal_id: str | None = None) -> None:
        triage = self._triage(case)
        emit(
            self.bus,
            "CaseUpdated",
            {
                **self._summary(case, triage, case.status),
                "reason": reason,
                **({"signalId": signal_id} if signal_id else {}),
            },
            component=COMPONENT,
            case_id=case.case_id,
            environment=self.env,
        )
        # New evidence starts reasoning only on a case no run has picked up yet; supplier
        # replies to a waiting case are matched by the dialogue service (M4), not here.
        if case.status is CaseStatus.TRIAGED:
            emit(
                self.bus,
                "CaseReadyForRun",
                {"caseId": case.case_id, "reason": f"new {reason}"},
                component=COMPONENT,
                case_id=case.case_id,
                environment=self.env,
            )

    def _attach(self, case: Case, signal: Signal) -> None:
        try:
            self.cases.add_signal(case.case_id, signal.signal_id)
        except self.dynamodb.exceptions.ConditionalCheckFailedException:
            pass  # already attached: a redelivered event
        self.signals.save(signal.model_copy(update={"case_id": case.case_id}))

    def _triage(self, case: Case) -> Triage:
        triage = assess(self.sap, case.material, case.plant, self.clock())
        self.cases.update_triage(
            case.case_id,
            rar_usd=triage.rar_usd,
            stockout_at=triage.stockout_at,
            priority_score=triage.priority_score,
            actor="system",
            figures=[f.model_dump(mode="json", by_alias=True) for f in triage.figures],
        )
        return triage

    @staticmethod
    def _summary(case: Case, triage: Triage, status: CaseStatus) -> dict[str, Any]:
        return {
            "caseId": case.case_id,
            "status": status.value,
            "stage": case.model_copy(update={"status": status}).stage,
            "material": case.material,
            "plant": case.plant,
            "priorityScore": str(triage.priority_score),
            "rarUsd": str(triage.rar_usd),
            "stockoutAt": triage.stockout_at.isoformat() if triage.stockout_at else None,
        }

    # SAP subjects -----------------------------------------------------------------------

    def _subject(self, material: str, plant: str, *, po_number: str | None) -> Subject:
        if po_number:
            found = self._subject_for_po(po_number, material)
            if found is not None and found.plant == plant:
                return found
        return Subject(material=material, plant=plant)

    def _subject_for_po(self, po_number: str, material: str | None) -> Subject | None:
        try:
            record = self.sap.get(
                PO_SERVICE,
                "A_PurchaseOrder",
                {"PurchaseOrder": po_number},
                expand="to_PurchaseOrderItem",
            )
        except SapNotFoundError:
            return None
        items = results(record.data.get("to_PurchaseOrderItem"))
        chosen = [i for i in items if material and i.get("Material") == material] or items
        if len(chosen) != 1:
            return None
        item = chosen[0]
        return Subject(
            material=str(item["Material"]),
            plant=str(item["Plant"]),
            po_number=po_number,
            po_item=str(item.get("PurchaseOrderItem") or "") or None,
            description=str(item.get("PurchaseOrderItemText") or "") or None,
        )

    # Lookup items -----------------------------------------------------------------------

    def _open_case(self, key: str) -> Case | None:
        item = self.dynamodb.get_item(
            TableName=self._table, Key={"PK": {"S": key}, "SK": {"S": "OPEN"}}, ConsistentRead=True
        ).get("Item")
        if item is None:
            return None
        case = self.cases.get(item["caseId"]["S"])
        if case is None or case.status in TERMINAL:
            return None
        return case

    def _put_key(self, key: str, case_id: str) -> None:
        self._put({"PK": key, "SK": "OPEN", "caseId": case_id})

    def _put(self, item: dict[str, Any]) -> None:
        self.dynamodb.put_item(TableName=self._table, Item=to_item(item))


def _material_key(material: str, plant: str) -> str:
    return f"KEY#MAT#{material}#PLANT#{plant}"


def _po_key(po_number: str, material: str) -> str:
    return f"KEY#PO#{po_number}#MAT#{material}"


def _po(element: Any) -> str | None:
    text = str(element or "")
    return text if text.isdigit() and text.startswith("45") and len(text) == 10 else None


def _days_late(message: dict[str, Any]) -> Decimal | None:
    try:
        element = date.fromisoformat(str(message["elementDate"])[:10])
        wanted = date.fromisoformat(str(message["reschedulingDate"])[:10])
    except (KeyError, ValueError):
        return None
    return Decimal(max((element - wanted).days, 0))


def _signal_case_type(signal: Signal) -> CaseType:
    if signal.channel is SignalChannel.CARRIER:
        return "CARRIER_DELAY"
    if any(f.name == "QUANTITY" for f in signal.fields):
        return "QUANTITY_SHORTFALL"
    return "SUPPLIER_DELAY"


_service: CaseService | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _service
    if _service is None:
        from services.shared import runtime

        _service = CaseService(
            dynamodb=runtime.client("dynamodb"),
            sap=runtime.sap_client(),
            bus=runtime.client("events"),
        )
    detail = event["detail"]
    if event.get("detail-type") == "MrpExceptionsPolled":
        return {"opened": _service.on_mrp(detail["data"])}
    return {"caseId": _service.on_signal(str(detail["data"]["signalId"]))}
