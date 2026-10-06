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

from services.api import whatif
from services.optimizer.runtime import (
    PortfolioService,
    Solver,
    allocation_for,
    candidate_id,
    conforms,
    resized_plan,
)
from services.routing.logic import Policy, Route, route
from services.routing.store import ControlStore
from services.shared.audit import AuditWriter
from services.shared.cases import CaseStore, ConcurrentUpdateError
from services.shared.config import Config
from services.shared.dynamo import table_name, to_item
from services.shared.models import Case, CaseStatus, PlanRecord, ProposedPlan
from services.shared.runtime import emit
from services.shared.sap_client import SapClient
from services.tools.context import ToolContext
from services.verifier.automated_reasoning import PolicyAssessment
from services.verifier.evidence import EvidenceReader
from services.verifier.grounding import sentences
from services.verifier.logic import Grounding, Verification, verify

COMPONENT = "verifier"
# (rationale, source, query) -> scores; the Lambda passes the Guardrail call (FR-VER-03).
GroundingCheck = Callable[[str, str, str], Grounding]
ReasoningCheck = Callable[[Verification, Route, Policy], PolicyAssessment]
NEXT_STATUS = {
    1: CaseStatus.AUTO_APPROVED,
    2: CaseStatus.AWAITING_APPROVAL,
    3: CaseStatus.ESCALATED,
}


# ADR-0042 (replaces the ADR-0040 wording): RELEVANCE scores how well the rationale answers
# this question. Measured live against the same sources, it gave real rationales 0.92-1.00
# and texts without a decision or without figures 0.00-0.22; the ADR-0040 wording, which named
# the material and plant, gave the same real rationales 0.19-0.99.
GROUNDING_QUERY = (
    "Which recovery actions were chosen: how many PC from which plant or supplier, "
    "arriving when, at what cost in USD?"
)


def grounding_rationale(plan: ProposedPlan) -> str:
    """The plan rationale and the chosen options' rationales, each sentence once (ADR-0042):
    the plan rationale usually repeats the options' own words, and a repeated sentence only
    skews RELEVANCE and the per-sentence mean G."""
    chosen = [o.rationale for o in plan.options if o.id in plan.chosen]
    return " ".join(dict.fromkeys(sentences(" ".join([plan.rationale, *chosen]))))


@dataclass
class VerifierService:
    dynamodb: Any
    sap: SapClient
    bus: Any
    grounding: GroundingCheck
    reasoning: ReasoningCheck | None = None
    scheduler: Any = None
    # BR-20: the portfolio solver (optimizer Lambda); None where no optimizer is deployed.
    solver: Solver | None = None
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
        item = self.control.get(case_id, f"PLAN#{plan_version}")
        if item is None:
            return {"skipped": "plan missing"}
        if (
            case.status is CaseStatus.INVESTIGATING
            and item.get("reallocatedFor")
            and case.active_run_id is None
        ):
            case = self._resume_reallocated(case_id) or case
        if case.status is not CaseStatus.PLAN_PROPOSED:
            return {"skipped": f"case is {case.status.value}"}
        for key in ("PK", "SK"):
            item.pop(key, None)
        record = PlanRecord.from_stored(item)
        # One verification moment: every re-read, projection and cover window is measured
        # from the same instant, so a boundary case (donor left at exactly its minimum
        # cover) does not fail on milliseconds between two clock readings (V-07).
        moment = self.clock()
        ctx = ToolContext(
            sap=self.sap,
            dynamodb=self.dynamodb,
            bus=self.bus,
            clock=lambda: moment,
            env=self.env,
            actor="verifier",
        )
        allocation = None
        reproposed: dict[str, str] = {}
        if self.solver is not None:
            portfolio = PortfolioService(
                ctx, self.solver, self.config.decimal("DONOR_MIN_COVER_DAYS")
            ).solve_for(case_id)
            allocation = allocation_for(portfolio, case_id)
            solved = (
                portfolio
                if portfolio is not None and portfolio["solverStatus"] in ("OPTIMAL", "FEASIBLE")
                else None
            )
            if (
                solved is not None
                and allocation
                and not item.get("resizedBy")
                and not conforms(record, allocation)
            ):
                revised = resized_plan(ctx, case, record, allocation)
                if revised is not None:
                    return self._propose_resized(revised, str(solved["portfolioId"]), moment)
            if solved is not None:
                # The case goes on to verification with this solve: the other members whose
                # approval it re-sized are re-proposed now (FR-OPZ-03, ADR-0044).
                reproposed = self._repropose_members(ctx, solved, case_id)
        facts = EvidenceReader(ctx).gather(case, record.plan)
        rationale = grounding_rationale(record.plan)
        verification = verify(
            record.plan,
            facts.evidence,
            now=moment,
            grounding=self.grounding(rationale, facts.source, GROUNDING_QUERY),
            corroboration=facts.corroboration,
            proposed_at=record.proposed_at,
            minimum_cover=self.config.decimal("DONOR_MIN_COVER_DAYS"),
            allocation=allocation,
        )
        projection = whatif.projection(ctx, case, None)
        self._record(case_id, plan_version, verification, projection)
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
        policy = self.policy()
        result = route(
            verification,
            now=now,
            stockout=facts.stockout,
            stockout_without=facts.stockout_without,
            plant=case.plant,
            limits=self.control.limits(case_id=case_id),
            policy=policy,
        )
        if self.reasoning is not None:
            assessment = self.reasoning(verification, result, policy)
            self.dynamodb.update_item(
                TableName=self._table,
                Key=to_item({"PK": f"CASE#{case_id}", "SK": f"PLAN#{plan_version}"}),
                UpdateExpression="SET automatedReasoning = :assessment",
                ExpressionAttributeValues=to_item(
                    {
                        ":assessment": {
                            "status": assessment.status,
                            "statements": assessment.statements,
                            "findings": assessment.findings,
                            "policyArn": assessment.policy_arn,
                        }
                    }
                ),
            )
            if result.tier != 3 and assessment.disagrees:
                result = Route(3, result.version_hash, reason="AUTOMATED_REASONING_DISAGREEMENT")
        self.control.save(verification, result, plant=case.plant, now=now)
        self._settle(case_id, result)
        outcome: dict[str, Any] = {
            "tier": result.tier,
            "reason": result.reason,
            "confidence": str(verification.record.confidence),
            "parts": [p.id for p in result.parts],
        }
        if reproposed:
            outcome["reproposed"] = reproposed
        return outcome

    def _repropose_members(
        self, ctx: ToolContext, portfolio: dict[str, Any], case_id: str
    ) -> dict[str, str]:
        """FR-OPZ-03 (ADR-0044): every other member that waits for approval on a plan this
        solve re-sized gets the optimizer's sizing as its next version, or escalates when
        nothing it can run is left. A plan that is already the optimizer's sizing is never
        re-proposed (loop guard), and a member whose chosen options were not all in the model
        is left alone: its allocation does not describe its plan."""
        portfolio_id = str(portfolio["portfolioId"])
        candidates = {str(c["id"]) for c in portfolio.get("candidateActions") or []}
        outcomes: dict[str, str] = {}
        for member in portfolio["caseIds"]:
            other = self.cases.get(member) if member != case_id else None
            if other is None or other.status is not CaseStatus.AWAITING_APPROVAL:
                continue
            item = self.control.get(member, f"PLAN#{other.plan_version}")
            if item is None or item.get("resizedBy"):
                continue
            for key in ("PK", "SK"):
                item.pop(key, None)
            record = PlanRecord.from_stored(item)
            if any(candidate_id(member, o) not in candidates for o in record.plan.chosen):
                continue
            allocation = allocation_for(portfolio, member) or {}
            if conforms(record, allocation):
                continue
            revised = resized_plan(ctx, other, record, allocation)
            stored = (
                None
                if revised is None
                else {
                    **PlanRecord(plan=revised, proposed_at=ctx.now()).model_dump(
                        mode="json", by_alias=True
                    ),
                    "resizedBy": "optimizer",
                    "portfolioId": portfolio_id,
                    "reallocatedFor": case_id,
                }
            )
            outcomes[member] = self.control.reallocate(
                other, stored, portfolio_id=portfolio_id, trigger=case_id, now=ctx.now()
            )
        return outcomes

    def _resume_reallocated(self, case_id: str) -> Case | None:
        """The second half of a re-proposal: the case left AWAITING_APPROVAL for INVESTIGATING
        in the reallocation transaction; its `PlanProposed` moves it on to PLAN_PROPOSED.
        A duplicate delivery that lost the race reads the state the winner left."""
        try:
            return self.cases.transition(
                case_id,
                CaseStatus.PLAN_PROPOSED,
                actor="system",
                reason="PORTFOLIO_REALLOCATED",
                expected=CaseStatus.INVESTIGATING,
            )
        except ConcurrentUpdateError:
            return self.cases.get(case_id)

    def _propose_resized(
        self, plan: ProposedPlan, portfolio_id: str, now: datetime
    ) -> dict[str, Any]:
        """FR-OPZ-03: the optimizer's sizing becomes the next plan version, which is verified
        and routed like any proposal (the PlanProposed event brings it back here)."""
        case_id = plan.case_id
        record = PlanRecord(plan=plan, proposed_at=now)
        self.dynamodb.put_item(
            TableName=self._table,
            Item=to_item(
                {
                    "PK": f"CASE#{case_id}",
                    "SK": f"PLAN#{plan.plan_version}",
                    **record.model_dump(mode="json", by_alias=True),
                    "resizedBy": "optimizer",
                    "portfolioId": portfolio_id,
                }
            ),
            ConditionExpression="attribute_not_exists(PK)",
        )
        self.dynamodb.update_item(
            TableName=self._table,
            Key={"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": "META"}},
            UpdateExpression="SET planVersion = :v",
            ConditionExpression="planVersion = :old",
            ExpressionAttributeValues={
                ":v": {"N": str(plan.plan_version)},
                ":old": {"N": str(plan.plan_version - 1)},
            },
        )
        self.audit.record(
            f"CASE#{case_id}",
            "PLAN_RESIZED",
            actor="system",
            case_id=case_id,
            payload={
                "planVersion": plan.plan_version,
                "portfolioId": portfolio_id,
                "resizedBy": "optimizer",
            },
        )
        self._emit("PlanProposed", case_id, {"planVersion": plan.plan_version})
        return {"resized": plan.plan_version, "portfolioId": portfolio_id}

    def _record(
        self,
        case_id: str,
        version: int,
        verification: Verification,
        projection: dict[str, Any],
    ) -> None:
        record = verification.record
        self.dynamodb.update_item(
            TableName=self._table,
            Key=to_item({"PK": f"CASE#{case_id}", "SK": f"PLAN#{version}"}),
            UpdateExpression=(
                "SET checks = :checks, confidence = :confidence, verifiedAt = :at, "
                "#projection = :projection"
            ),
            ExpressionAttributeNames={"#projection": "projection"},
            ExpressionAttributeValues=to_item(
                {
                    ":checks": [c.model_dump(mode="json", by_alias=True) for c in record.checks],
                    ":confidence": record.confidence or Decimal(0),
                    ":at": record.verified_at.isoformat() if record.verified_at else None,
                    ":projection": projection,
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
