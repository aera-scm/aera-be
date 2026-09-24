"""Monitor: goods-receipt check, then close or reopen (SRD 6.25.2 step 5; FR-MON-01..03,
BR-14, FR-MET-01).

On `GoodsReceiptDue` for an executed plan part, SAP material documents (movement 101) are
read for the documents that part should bring in: the STO it created and the purchase order
it expedited. All received: the part is marked, and when every executed part is received the
case closes with its metrics. Anything missing: the case reopens with the reason, and
rollback is offered (FR-MON-03).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from services.execution.journal import Journal
from services.routing.store import ControlStore
from services.shared.audit import AuditWriter
from services.shared.case_state import can_transition
from services.shared.cases import CaseStore, ConcurrentUpdateError, IllegalTransitionError
from services.shared.dynamo import table_name, to_item
from services.shared.metrics import case_metrics
from services.shared.models import CaseStatus
from services.shared.runtime import emit
from services.shared.sap_client import SapClient
from services.shared.sap_values import number
from services.shared.triage import odata_quote

COMPONENT = "monitor"
DOCUMENTS = "API_MATERIAL_DOCUMENT_SRV"


@dataclass
class Monitor:
    dynamodb: Any
    sap: SapClient
    bus: Any
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    env: str | None = None

    def __post_init__(self) -> None:
        self.cases = CaseStore(self.dynamodb, self.env, clock=self.clock)
        self.control = ControlStore(self.dynamodb, self.env)
        self.journal = Journal(self.dynamodb, self.env)
        self.audit = AuditWriter(self.dynamodb, self.env)
        self._cases = table_name("cases", self.env)

    def expected_receipts(self, case_id: str, part_id: str) -> list[tuple[str, Decimal]]:
        """(purchasing document, quantity) pairs the executed part must bring in."""
        part = self.control.get(case_id, f"PART#{part_id}")
        if part is None:
            return []
        record = self.journal.read(f"EXEC#{case_id}#{part['version']}#{part_id}")
        if record is None or record.get("status") != "SUCCEEDED":
            return []
        expected: list[tuple[str, Decimal]] = []
        # The journal keeps each executed step with its result, in execution order.
        for step, result in zip(
            record["request"]["steps"], record["result"]["results"], strict=True
        ):
            action = step["action"]
            if action["type"] == "CREATE_STO":
                expected.append((str(result["document"]), Decimal(str(action["qty"]))))
            elif action["type"] == "BOOK_AIR_FREIGHT":
                expected.append((str(action["poNumber"]), Decimal(str(action["qty"]))))
        return expected

    def received(self, document: str) -> Decimal:
        rows = self.sap.query(
            DOCUMENTS,
            "A_MaterialDocumentItem",
            filter=f"PurchaseOrder eq {odata_quote(document)} and GoodsMovementType eq '101'",
        )
        return sum((number(r.data.get("QuantityInBaseUnit")) for r in rows), Decimal(0))

    def check(self, case_id: str, part_id: str) -> dict[str, Any]:
        missing = [
            {"document": doc, "expected": str(qty), "received": str(got)}
            for doc, qty in self.expected_receipts(case_id, part_id)
            if (got := self.received(doc)) < qty
        ]
        if missing:
            return self._reopen(case_id, part_id, missing)
        self.dynamodb.put_item(
            TableName=self._cases,
            Item=to_item(
                {
                    "PK": f"CASE#{case_id}",
                    "SK": f"RECEIVED#{part_id}",
                    "checkedAt": self.clock().isoformat(),
                }
            ),
        )
        self.audit.record(
            f"CASE#{case_id}",
            "GOODS_RECEIPT_POSTED",
            actor="system",
            case_id=case_id,
            payload={"planPartId": part_id},
        )
        if self._all_received(case_id):
            return self._close(case_id)
        return {"outcome": "RECEIVED"}

    def _all_received(self, case_id: str) -> bool:
        meta = self.control.get(case_id, "META")
        route = self.control.get(case_id, f"ROUTE#{meta['planVersion']}") if meta else None
        if route is None:
            return False
        for part in route["parts"]:
            record = self.journal.read(f"EXEC#{case_id}#{route['version']}#{part['id']}")
            executed = record is not None and record.get("status") == "SUCCEEDED"
            if executed and self.control.get(case_id, f"RECEIVED#{part['id']}") is None:
                return False
            if not executed and part.get("decision") != "REJECTED" and part["tier"] == 2:
                return False  # a part still waiting for approval keeps the case open
        return True

    def _move(self, case_id: str, status: CaseStatus, reason: str) -> bool:
        case = self.cases.get(case_id)
        if case is None or not can_transition(case.status, status):
            return False
        try:
            self.cases.transition(case_id, status, actor="system", reason=reason)
        except (IllegalTransitionError, ConcurrentUpdateError):
            return False
        return True

    def _close(self, case_id: str) -> dict[str, Any]:
        case = self.cases.get(case_id)
        if case is None or not self._move(case_id, CaseStatus.CLOSED, "goods received"):
            return {"outcome": "RECEIVED"}
        metrics = case_metrics(self.dynamodb, case_id, case.created_at, self.clock(), self.env)
        self.dynamodb.put_item(
            TableName=self._cases,
            Item=to_item({"PK": f"CASE#{case_id}", "SK": "METRICS", **metrics}),
        )
        emit(
            self.bus,
            "CaseClosed",
            {"caseId": case_id, "reason": "goods received", "metrics": metrics},
            component=COMPONENT,
            case_id=case_id,
            environment=self.env,
        )
        return {"outcome": "CLOSED", "metrics": metrics}

    def _reopen(self, case_id: str, part_id: str, missing: list[dict[str, str]]) -> dict[str, Any]:
        reason = "goods receipt missing: " + ", ".join(
            f"{m['document']} ({m['received']} of {m['expected']})" for m in missing
        )
        reopened = self._move(case_id, CaseStatus.REOPENED, reason)
        self.audit.record(
            f"CASE#{case_id}",
            "GOODS_RECEIPT_MISSING",
            actor="system",
            case_id=case_id,
            payload={"planPartId": part_id, "missing": missing},
        )
        emit(
            self.bus,
            "CaseReopened" if reopened else "CaseUpdated",
            {"caseId": case_id, "reason": reason, "rollbackAvailable": reopened},
            component=COMPONENT,
            case_id=case_id,
            environment=self.env,
        )
        return {"outcome": "REOPENED" if reopened else "MISSING", "reason": reason}


_monitor: Monitor | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _monitor
    if _monitor is None:
        from services.shared import runtime

        _monitor = Monitor(
            dynamodb=runtime.client("dynamodb"),
            sap=runtime.sap_client(),
            bus=runtime.client("events"),
        )
    data = (event.get("detail") or {}).get("data") or {}
    return _monitor.check(str(data["caseId"]), str(data["planPartId"]))
