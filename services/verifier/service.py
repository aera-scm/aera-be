"""Verifier and routing at runtime: `PlanProposed` in, a routed plan out (SRD 6.6, 6.7).

1. Load the proposal and re-read every fact it rests on (evidence.py).
2. Run V-01..V-13 and BR-18 (logic.py); record checks and confidence on the plan.
3. Route in the same invocation (BR-05, BR-22, BR-23): routing accepts a verification at
   most 60 s old, and the evidence it needs is not persisted.
4. Persist the route atomically with its outbox (`PlanApproved` for Tier 1 parts,
   `PlanRouted`), move the case, and schedule the approval reminder and deadline.

Repeated events are harmless: a plan version is verified and routed once.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from services.routing.logic import Policy, Route, route
from services.routing.store import ControlStore
from services.shared.audit import AuditWriter
from services.shared.cases import CaseStore
from services.shared.config import Config
from services.shared.dynamo import table_name, to_item
from services.shared.models import CaseStatus, PlanRecord
from services.shared.runtime import emit
from services.shared.sap_client import SapClient
from services.tools.context import ToolContext
from services.verifier.evidence import EvidenceReader
from services.verifier.logic import Grounding, Verification, verify

COMPONENT = "verifier"
# (rationale, source, query) -> scores; the Lambda passes the Guardrail call (FR-VER-03).
GroundingCheck = Callable[[str, str, str], Grounding]
NEXT_STATUS = {
    1: CaseStatus.AUTO_APPROVED,
    2: CaseStatus.AWAITING_APPROVAL,
    3: CaseStatus.ESCALATED,
}


@dataclass
class VerifierService:
    dynamodb: Any
    sap: SapClient
    bus: Any
    grounding: GroundingCheck
    scheduler: Any = None
    timer_target_arn: str = ""
    scheduler_role_arn: str = ""
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    env: str | None = None

    def __post_init__(self) -> None:
        self.cases = CaseStore(self.dynamodb, self.env, clock=self.clock)
        self.control = ControlStore(self.dynamodb, self.env)
        self.config = Config(self.dynamodb, self.env)
        self.audit = AuditWriter(self.dynamodb, self.env)
        self._table = table_name("cases", self.env)

    def _emit(self, kind: Any, case_id: str, data: dict[str, Any]) -> None:
        data = {"caseId": case_id, **data}
        emit(self.bus, kind, data, component=COMPONENT, case_id=case_id, environment=self.env)

    def policy(self) -> Policy:
        return Policy(
            auto_limit=self.config.decimal("TIER1_MAX_USD"),
            confidence_min=self.config.decimal("TIER1_MIN_CONFIDENCE"),
            audit_share=self.config.decimal("AUDIT_SAMPLE_RATE"),
            approval_window=timedelta(hours=float(self.config.decimal("APPROVAL_MAX_HOURS"))),
            kill_switch=self.config.kill_switch(),
        )

    def handle(self, case_id: str, plan_version: int) -> dict[str, Any]:
        existing = self.control.get(case_id, f"ROUTE#{plan_version}")
        if existing is not None:
            return {"tier": existing["tier"], "replayed": True}
        case = self.cases.get(case_id)
        if case is None or case.plan_version != plan_version:
            return {"skipped": "not the current plan version"}
        if case.status is not CaseStatus.PLAN_PROPOSED:
            return {"skipped": f"case is {case.status.value}"}
        item = self.control.get(case_id, f"PLAN#{plan_version}")
        if item is None:
            return {"skipped": "plan missing"}
        for key in ("PK", "SK"):
            item.pop(key, None)
        record = PlanRecord.model_validate(item)
        ctx = ToolContext(
            sap=self.sap,
            dynamodb=self.dynamodb,
            bus=self.bus,
            clock=self.clock,
            env=self.env,
            actor="verifier",
        )
        facts = EvidenceReader(ctx).gather(case, record.plan)
        chosen = [o for o in record.plan.options if o.id in record.plan.chosen]
        rationale = " ".join([record.plan.rationale, *(o.rationale for o in chosen)])
        query = f"Recovery plan for {case.material} at plant {case.plant} ({case_id})"
        verification = verify(
            record.plan,
            facts.evidence,
            now=self.clock(),
            grounding=self.grounding(rationale, facts.source, query),
            corroboration=facts.corroboration,
            proposed_at=record.proposed_at,
            minimum_cover=self.config.decimal("DONOR_MIN_COVER_DAYS"),
        )
        self._record(case_id, plan_version, verification)
        self.cases.transition(
            case_id,
            CaseStatus.VERIFIED,
            actor="system",
            reason="verified",
            expected=CaseStatus.PLAN_PROPOSED,
        )
        failed = sorted(
            {
                f"{c.check_id}{'/' + c.option_id if c.option_id else ''}"
                for c in verification.record.checks
                if c.blocking and not c.passed
            }
        )
        self._emit(
            "PlanVerified",
            case_id,
            {
                "planVersion": plan_version,
                "planVersionHash": verification.version_hash,
                "confidence": str(verification.record.confidence),
                "failedChecks": failed,
            },
        )
        now = self.clock()
        result = route(
            verification,
            now=now,
            stockout=facts.stockout,
            plant=case.plant,
            limits=self.control.limits(),
            policy=self.policy(),
        )
        self.control.save(verification, result, plant=case.plant, now=now)
        self._settle(case_id, result)
        return {
            "tier": result.tier,
            "reason": result.reason,
            "confidence": str(verification.record.confidence),
            "parts": [p.id for p in result.parts],
        }

    def _record(self, case_id: str, version: int, verification: Verification) -> None:
        record = verification.record
        self.dynamodb.update_item(
            TableName=self._table,
            Key=to_item({"PK": f"CASE#{case_id}", "SK": f"PLAN#{version}"}),
            UpdateExpression="SET checks = :checks, confidence = :confidence, verifiedAt = :at",
            ExpressionAttributeValues=to_item(
                {
                    ":checks": [c.model_dump(mode="json", by_alias=True) for c in record.checks],
                    ":confidence": record.confidence or Decimal(0),
                    ":at": record.verified_at.isoformat() if record.verified_at else None,
                }
            ),
        )
        self.dynamodb.update_item(
            TableName=self._table,
            Key=to_item({"PK": f"CASE#{case_id}", "SK": "META"}),
            UpdateExpression="SET confidence = :confidence",
            ExpressionAttributeValues=to_item({":confidence": record.confidence or Decimal(0)}),
        )

    def _settle(self, case_id: str, result: Route) -> None:
        target = NEXT_STATUS[result.tier]
        self.dynamodb.update_item(
            TableName=self._table,
            Key=to_item({"PK": f"CASE#{case_id}", "SK": "META"}),
            UpdateExpression="SET tier = :tier",
            ExpressionAttributeValues=to_item({":tier": result.tier}),
        )
        self.cases.transition(
            case_id,
            target,
            actor="system",
            reason=result.reason or f"Tier {result.tier}",
            expected=CaseStatus.VERIFIED,
        )
        for part in result.parts:
            if part.tier != 2 or part.deadline is None or part.reminder is None:
                continue
            self._emit(
                "NotificationRequested",
                case_id,
                {
                    "recipientRole": "approver",
                    "approverId": part.approver,
                    "templateId": "APPROVAL_REQUEST",
                    "planPartId": part.id,
                },
            )
            for kind, at in (("reminder", part.reminder), ("deadline", part.deadline)):
                self._timer(case_id, part.id, kind, at)

    def _timer(self, case_id: str, part_id: str, kind: str, at: datetime) -> None:
        """BR-23: one-shot schedules that invoke the routing timer for this part."""
        if self.scheduler is None:
            return
        due = max(at, self.clock() + timedelta(minutes=1))
        try:
            self.scheduler.create_schedule(
                Name=f"aera-{self.env or 'dev'}-appr-{part_id[:16]}-{kind}",
                ScheduleExpression=f"at({due.astimezone(UTC):%Y-%m-%dT%H:%M:%S})",
                FlexibleTimeWindow={"Mode": "OFF"},
                ActionAfterCompletion="DELETE",
                Target={
                    "Arn": self.timer_target_arn,
                    "RoleArn": self.scheduler_role_arn,
                    "Input": json.dumps({"caseId": case_id, "planPartId": part_id}),
                },
            )
        except self.scheduler.exceptions.ConflictException:
            pass  # a retried event: the timer exists
