"""Plan, route and execution evidence for the console case detail (SRD 6.10, FR-UI-07,
FR-UI-08, FR-UI-10, FR-RTE-02, FR-RTE-07, FR-RTE-08).

Only stored records are returned, for the case's current plan version; nothing is computed
from SAP here. A missing record stays absent so the console can say so (ADR-012).
"""

from typing import Any

from services.execution.journal import Journal, idempotency_key
from services.routing.store import ControlStore
from services.shared.audit import AuditWriter
from services.shared.models import (
    Case,
    ExecutionView,
    PlanPartView,
    PlanRecord,
    PlanView,
    ProposedPlan,
    RouteView,
    UndoStep,
)
from services.verifier.logic import REVERSIBLE

# SRD 6.8 undo per action type; BR-17 decides which actions are reversible.
UNDO = {
    "CREATE_STO": "Set the deletion indicator on the STO item before goods issue.",
    "CHANGE_PO_DATE": "Restore the stored previous delivery date.",
    "SPLIT_PO_SCHEDULE_LINE": "Delete the new schedule line and restore the original quantity.",
    "BOOK_AIR_FREIGHT": "Not reversible once booked; the freight cost stays.",
    "CREATE_PO_ALTERNATE": "Not reversible: a new purchase order to another supplier.",
}
MILESTONES = frozenset(
    {
        "UNDO_SAVED",
        "EXECUTION_REPLAY",
        "EXECUTION_COMPLETED",
        "EXECUTION_HALTED",
        "EXECUTION_UNCERTAIN",
        "FAILED_ROLLED_BACK",
        "ACTION_COMPENSATED",
        "ACTION_IRREVERSIBLE",
        "COMPENSATION_FAILED",
        "ROLLED_BACK",
    }
)


def undo_summary(plan: ProposedPlan, option_ids: list[str]) -> list[UndoStep]:
    options = {option.id: option for option in plan.options}
    return [
        UndoStep(
            option_id=option_id,
            action_type=action.type,
            reversible=action.type in REVERSIBLE,
            undo=UNDO[action.type],
        )
        for option_id in option_ids
        for action in options[option_id].actions
    ]


def _plan(item: dict[str, Any], version_hash: str | None) -> PlanView:
    # Other attributes on the item (projection, policy statements) are not in the contract.
    data = PlanRecord.from_stored(item).model_dump(mode="json", by_alias=True)
    reasoning = item.get("automatedReasoning")
    if reasoning is not None:
        data["automatedReasoning"] = {
            "status": reasoning["status"],
            "findings": list(reasoning.get("findings") or []),
        }
    return PlanView.model_validate({**data, "planVersionHash": version_hash})


def _part(stored: dict[str, Any], plan: ProposedPlan) -> PlanPartView:
    payload = stored.get("decisionPayload") or {}
    return PlanPartView(
        plan_part_id=stored["id"],
        options=list(stored["options"]),
        tier=int(stored["tier"]),
        cost_usd=stored["cost"],
        confidence=stored.get("confidence"),
        sampled=bool(stored.get("sampled", False)),
        approver_id=stored.get("approver"),
        backup_approver_id=stored.get("backup"),
        deadline_at=stored.get("deadline"),
        reminder_at=stored.get("reminder"),
        reminded=bool(stored.get("reminded", False)),
        expired=bool(stored.get("expired", False)),
        decision=stored.get("decision"),
        comment=payload.get("comment"),
        decided_at=stored.get("decidedAt"),
        undo_summary=undo_summary(plan, list(stored["options"])),
    )


class CaseView:
    def __init__(self, client: Any, env: str | None = None) -> None:
        self.control = ControlStore(client, env)
        self.journal = Journal(client, env)
        self.audit = AuditWriter(client, env)

    def read(self, case: Case) -> dict[str, Any]:
        version = case.plan_version
        empty: dict[str, Any] = {"plan": None, "route": None, "execution": []}
        if not version:
            return empty
        case_id = case.case_id
        plan_item = self.control.get(case_id, f"PLAN#{version}")
        if plan_item is None:
            return empty
        route_item = self.control.get(case_id, f"ROUTE#{version}")
        plan = _plan(plan_item, route_item["versionHash"] if route_item else None)
        if route_item is None:
            return {**empty, "plan": plan.model_dump(mode="json", by_alias=True)}
        verified = ProposedPlan.model_validate(route_item["verifiedPlan"]["plan"])
        parts = [
            _part(self.control.get(case_id, f"PART#{part['id']}") or part, verified)
            for part in route_item["parts"]
        ]
        route = RouteView(
            plan_version=version,
            plan_version_hash=route_item["versionHash"],
            tier=int(route_item["tier"]),
            reason=route_item.get("reason"),
            routed_at=route_item["routedAt"],
            parts=parts,
        )
        return {
            "plan": plan.model_dump(mode="json", by_alias=True),
            "route": route.model_dump(mode="json", by_alias=True),
            "execution": [
                view.model_dump(mode="json", by_alias=True)
                for view in self._executions(case_id, version, [p.plan_part_id for p in parts])
            ],
        }

    def _executions(self, case_id: str, version: int, part_ids: list[str]) -> list[ExecutionView]:
        records = {
            part_id: record
            for part_id in part_ids
            if (record := self.journal.read(f"EXEC#{case_id}#{version}#{part_id}")) is not None
        }
        if not records:
            return []
        events = [e for e in self.audit.events(f"CASE#{case_id}") if e.type in MILESTONES]
        views = []
        for part_id, record in records.items():
            steps = []
            keys = set()
            for step in record["request"]["steps"]:
                action = step["action"]
                key = idempotency_key(
                    case_id, version, int(step["index"]), action["type"], step["target"]
                )
                keys.add(key)
                journal = self.journal.read(key) or {}
                result = journal.get("result") or {}
                undo = journal.get("undo") or {}
                document = result.get("document")
                steps.append(
                    {
                        "index": int(step["index"]),
                        "actionType": action["type"],
                        "target": step["target"],
                        "status": journal.get("status"),
                        "sapDocument": None if document is None else str(document),
                        "sourceRef": result.get("sourceRef"),
                        "undoType": undo.get("type"),
                        "irreversible": bool(undo.get("irreversible", False)),
                    }
                )
            views.append(
                ExecutionView.model_validate(
                    {
                        "planPartId": part_id,
                        "status": record["status"],
                        "steps": steps,
                        "milestones": [
                            {"type": e.type, "ts": e.ts}
                            for e in events
                            # Compensation events name the step key instead of the part.
                            if e.payload.get("partId") == part_id or e.payload.get("key") in keys
                        ],
                    }
                )
            )
        return views
