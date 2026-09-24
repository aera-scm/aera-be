"""One agent run of one case (SRD 6.3, 6.18, BR-11, NFR-REL-04).

The run-starter has already claimed the case (`activeRunId`) and moved it to INVESTIGATING.
The harness loads the case context, runs the supervisor with its hooks, and whatever
happens — plan, question, escalation, a tripped limit or an error — records the run, releases
the case and emits `RunEnded`. A limit or a run that ends without a terminal tool is
escalated by the harness, never by the model; two consecutive failures escalate too.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from strands import Agent
from strands.models import Model

from services.agent.hooks import LimitHook, Limits, TerminationHook, TraceHook
from services.agent.prompt import PROMPT_VERSION, opening_message, system_prompt
from services.shared.models import CaseStatus
from services.shared.observability import get_logger
from services.shared.runs import RunStore
from services.shared.runtime import emit
from services.shared.trace import TraceStore
from services.tools.case_tools import escalate
from services.tools.context import ToolContext

COMPONENT = "agent"
_log = get_logger("agent")


@dataclass
class RunOutcome:
    end_reason: str
    summary: str
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class Harness:
    ctx: ToolContext
    model: Model
    tools: Callable[[ToolContext], list[Any]]
    limits: Limits = field(default_factory=Limits)
    monotonic: Callable[[], float] = time.monotonic
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def run(self, payload: dict[str, Any]) -> RunOutcome:
        case_id = str(payload["caseId"])
        run_id = str(payload["runId"])
        ctx = self.ctx
        ctx.run_id = run_id
        runs = RunStore(ctx.dynamodb, ctx.env, clock=self.clock)
        trace = TraceHook(TraceStore(ctx.dynamodb, ctx.env), case_id, run_id, clock=self.clock)
        limit = LimitHook(self.limits, clock=self.monotonic)
        termination = TerminationHook()
        trace.system("Run started", f"prompt {PROMPT_VERSION}", mode=payload.get("mode"))
        end_reason = "LIMIT_ERROR"
        usage: dict[str, Any] = {}
        try:
            case = ctx.cases.get(case_id)
            if case is None or case.status is not CaseStatus.INVESTIGATING:
                raise RuntimeError(f"case {case_id} is not under investigation")
            agent = Agent(
                model=self.model,
                tools=self.tools(ctx),
                system_prompt=system_prompt(),
                hooks=[trace, limit, termination],
                callback_handler=None,
            )
            agent(
                opening_message(
                    case,
                    runs.history(case_id),
                    mode=str(payload.get("mode", "investigate")),
                    reason=payload.get("reason"),
                )
            )
            usage = dict(getattr(agent.event_loop_metrics, "accumulated_usage", {}) or {})
            if termination.ended is not None:
                end_reason = termination.ended
            elif limit.tripped is not None:
                end_reason = limit.tripped
                self._escalate(case_id, f"{limit.tripped}: run limit reached (BR-11)")
            else:
                end_reason = "ESCALATE"
                self._escalate(case_id, "the run ended without a plan, question or escalation")
        except Exception as error:  # noqa: BLE001 - any failure must end the run visibly
            _log.exception("agent run failed", extra={"caseId": case_id, "runId": run_id})
            trace.system("Run failed", type(error).__name__)
            end_reason = "LIMIT_ERROR"
        summary = self._summary(trace, end_reason)
        runs.release(case_id, run_id, end_reason=end_reason, summary=summary, usage=usage)
        if end_reason == "LIMIT_ERROR" and runs.consecutive_failures(case_id) >= 2:
            self._escalate(case_id, "two consecutive runs failed")
        trace.system("Run ended", end_reason)
        emit(
            ctx.bus,
            "RunEnded",
            {
                "caseId": case_id,
                "runId": run_id,
                "endReason": end_reason,
                "promptVersion": PROMPT_VERSION,
                "iterations": limit.iterations,
                "usage": usage,
            },
            component=COMPONENT,
            case_id=case_id,
            run_id=run_id,
            actor="agent",
            environment=ctx.env,
        )
        return RunOutcome(end_reason=end_reason, summary=summary, usage=usage)

    def _escalate(self, case_id: str, reason: str) -> None:
        case = self.ctx.cases.get(case_id)
        if case is not None and case.status is CaseStatus.INVESTIGATING:
            escalate(self.ctx, case_id, reason)

    @staticmethod
    def _summary(trace: TraceHook, end_reason: str) -> str:
        steps = ", ".join(trace.calls) or "no tool calls"
        last = trace.thoughts[-1] if trace.thoughts else ""
        return f"Ended {end_reason} after: {steps}. Last note: {last}".strip()
