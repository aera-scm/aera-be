"""Planner chat, "Ask AERA" (FR-CHT-01, FR-CHT-03, FR-CHT-04, BR-16, UC-12, AT-10).

Chat can explain and can start a constrained re-plan. It can never execute, approve, or
change a tier, threshold, allowlist or approval requirement: those requests are declined
with the reason, and what the approver already has is pointed to. Every message passes the
prompt-attack guardrail first. Replies are built from case records, not by a model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from services.rules.br_16 import read
from services.run_starter.handler import STARTABLE
from services.shared.audit import AuditWriter
from services.shared.cases import CaseStore
from services.shared.config import Config
from services.shared.dynamo import from_item, table_name, to_item
from services.shared.models import Case, new_ulid
from services.shared.runtime import emit

MAX_CHARS = 2000


def usd(value: Any) -> str:
    return f"USD {Decimal(str(value)):,.0f}"


@dataclass
class Chat:
    dynamodb: Any
    bus: Any
    scan: Callable[[str], str | None] = field(default=lambda text: None)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    env: str | None = None

    def __post_init__(self) -> None:
        self.cases = CaseStore(self.dynamodb, self.env)
        self.audit = AuditWriter(self.dynamodb, self.env)
        self.config = Config(self.dynamodb, self.env)
        self._table = table_name("cases", self.env)

    def _get(self, case_id: str, sk: str) -> dict[str, Any] | None:
        item = self.dynamodb.get_item(
            TableName=self._table,
            Key={"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": sk}},
            ConsistentRead=True,
        ).get("Item")
        return from_item(item, keep_decimals=False) if item else None

    def handle(self, case: Case, user_id: str, message: str) -> dict[str, Any]:
        message = message.strip()[:MAX_CHARS]
        actor = f"user:{user_id}"
        blocked = self.scan(message)
        if blocked:
            self._record(case.case_id, actor, message, refused=True)
            self.audit.record(
                f"CASE#{case.case_id}",
                "CHAT_BLOCKED",
                actor=actor,
                case_id=case.case_id,
                payload={"reason": blocked},
            )
            return {"reply": "This message was blocked by the input guardrail.", "refused": True}
        intent = read(message)
        replies: list[str] = []
        refused = False
        if intent.governance_change:
            refused = True
            replies.append(
                "Chat cannot change tiers, thresholds, allowlists or approval requirements "
                "(BR-16). An administrator changes configuration; an approver decides approvals."
            )
        if intent.execute:
            refused = True
            replies.extend(self._execution_answer(case))
        run_id = None
        if intent.replan:
            run_id, text = self._replan(case, actor, intent.constraints)
            replies.append(text)
        if not replies:
            replies.append(self._summary(case))
        self._record(case.case_id, actor, message, refused=refused)
        if refused:
            self.audit.record(
                f"CASE#{case.case_id}",
                "CHAT_REFUSED",
                actor=actor,
                case_id=case.case_id,
                payload={"execute": intent.execute, "governance": intent.governance_change},
            )
        return {"reply": "\n".join(replies), "refused": refused, "replanRunId": run_id}

    def _execution_answer(self, case: Case) -> list[str]:
        limit = self.config.decimal("TIER1_MAX_USD")
        route = self._get(case.case_id, f"ROUTE#{case.plan_version}") if case.plan_version else None
        if route is None:
            return ["Chat cannot execute (BR-16). A plan runs only after verification and routing."]
        answer = []
        for part in route["parts"]:
            options = ", ".join(part["options"])
            if part["tier"] == 2:
                answer.append(
                    f"Execution above {usd(limit)} needs approval (BR-05): options {options} "
                    f"({usd(part['cost'])}) are routed to {part.get('approver') or 'an approver'}"
                    ". Chat cannot execute or approve (BR-16)."
                )
            else:
                answer.append(
                    f"Options {options} ({usd(part['cost'])}) qualify for Tier 1 on their own "
                    "and run without approval."
                )
        return answer

    def _replan(
        self, case: Case, actor: str, constraints: dict[str, str]
    ) -> tuple[str | None, str]:
        described = ", ".join(f"{k}={v}" for k, v in constraints.items())
        if case.status not in STARTABLE or case.active_run_id:
            return None, (
                f"Re-planning under {described} can start once the case is not waiting for an "
                f"approval or a run (it is {case.status.value}); the current routing stands."
            )
        run_id = new_ulid()
        self.dynamodb.put_item(
            TableName=self._table,
            Item=to_item(
                {
                    "PK": f"CASE#{case.case_id}",
                    "SK": "CONSTRAINTS",
                    **constraints,
                    "setBy": actor,
                    "setAt": self.clock().isoformat(),
                }
            ),
        )
        emit(
            self.bus,
            "CaseReadyForRun",
            {
                "caseId": case.case_id,
                "reason": "planner constraints",
                "mode": "replan",
                "runId": run_id,
                "constraints": constraints,
            },
            component="api",
            case_id=case.case_id,
            actor=actor,
            environment=self.env,
        )
        return run_id, f"Re-planning under {described}. Routing and approvals apply as usual."

    @staticmethod
    def _summary(case: Case) -> str:
        parts = [f"{case.case_id} is {case.status.value}."]
        if case.stockout_at:
            parts.append(f"Projected stock-out {case.stockout_at.isoformat()}.")
        if case.rar_usd is not None:
            parts.append(f"Revenue at risk {usd(case.rar_usd)}.")
        return " ".join(parts)

    def _record(self, case_id: str, actor: str, message: str, *, refused: bool) -> None:
        self.dynamodb.put_item(
            TableName=self._table,
            Item=to_item(
                {
                    "PK": f"CASE#{case_id}",
                    "SK": f"CHAT#{new_ulid()}",
                    "actor": actor,
                    "message": message,
                    "refused": refused,
                    "at": self.clock().isoformat(),
                }
            ),
        )
