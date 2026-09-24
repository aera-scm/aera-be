"""Agent loop hooks (SRD 6.18, 6.3.4, BR-11, FR-AUD-03).

- `TraceHook`: a trace event before and after every tool call and the model's one-sentence
  "thought" after each turn; hidden reasoning is never stored.
- `LimitHook`: iterations, tokens and wall clock (BR-11); a tripped limit cancels the next
  model call and the harness escalates. Limits never fail silently.
- `TerminationHook`: the run ends once `propose_plan` is accepted, or `ask_planner` or
  `escalate` succeed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from strands.hooks import (
    AfterModelCallEvent,
    AfterToolCallEvent,
    AfterToolsEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from services.shared.models import TraceEvent
from services.shared.trace import TraceStore

END_REASONS = {
    "propose_plan": "PLAN",
    "escalate": "ESCALATE",
    "ask_planner": "WAITING_PLANNER",
    "request_supplier_info": "WAITING_SUPPLIER",
}
THOUGHT_CHARS = 300


def tool_name(raw: str) -> str:
    """Gateway tools arrive as `<target>___<tool>`."""
    return raw.split("___")[-1]


def result_payload(result: Any) -> dict[str, Any]:
    for block in (result or {}).get("content") or []:
        if "json" in block and isinstance(block["json"], dict):
            return dict(block["json"])
        if "text" in block:
            try:
                parsed = json.loads(block["text"])
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


@dataclass
class TraceHook(HookProvider):
    trace: TraceStore
    case_id: str
    run_id: str
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    calls: list[str] = field(default_factory=list)
    thoughts: list[str] = field(default_factory=list)

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)
        registry.add_callback(AfterModelCallEvent, self.after_model)

    def _write(self, kind: str, title: str, detail: str | None, data: dict[str, Any]) -> None:
        self.trace.write(
            TraceEvent(
                case_id=self.case_id,
                run_id=self.run_id,
                kind=kind,  # type: ignore[arg-type]
                title=title,
                detail=detail,
                data=data,
                ts=self.clock(),
            )
        )

    def system(self, title: str, detail: str | None = None, **data: Any) -> None:
        self._write("SYSTEM", title, detail, data)

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        name = tool_name(event.tool_use["name"])
        self.calls.append(name)
        arguments = event.tool_use.get("input") or {}
        self._write("TOOL_CALL", name, None, {"input": _short(arguments)})

    def after_tool(self, event: AfterToolCallEvent) -> None:
        name = tool_name(event.tool_use["name"])
        payload = result_payload(event.result)
        failed = event.result.get("status") == "error" or "error" in payload
        detail = str(payload.get("error")) if "error" in payload else None
        self._write(
            "TOOL_RESULT",
            name,
            detail,
            {"status": "error" if failed else "ok", "keys": sorted(payload)[:20]},
        )

    def after_model(self, event: AfterModelCallEvent) -> None:
        if event.stop_response is None:
            return
        texts = [
            block["text"].strip()
            for block in event.stop_response.message.get("content", [])
            if isinstance(block, dict) and block.get("text", "").strip()
        ]
        if texts:
            thought = texts[0][:THOUGHT_CHARS]
            self.thoughts.append(thought)
            self._write("AGENT", thought, None, {})


def _short(value: Any) -> Any:
    text = json.dumps(value, default=str)
    return value if len(text) <= 2000 else {"truncated": text[:2000]}


@dataclass
class Limits:
    max_iterations: int = 20
    max_tokens: int = 150_000
    max_seconds: float = 300.0


@dataclass
class LimitHook(HookProvider):
    limits: Limits
    clock: Callable[[], float] = time.monotonic
    iterations: int = 0
    tripped: str | None = None
    started: float | None = None

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before_model)

    def tokens_used(self, agent: Any) -> int:
        usage = getattr(getattr(agent, "event_loop_metrics", None), "accumulated_usage", {}) or {}
        return int(usage.get("totalTokens", 0))

    def before_model(self, event: BeforeModelCallEvent) -> None:
        now = self.clock()
        if self.started is None:
            self.started = now
        if self.tripped is None:
            if self.iterations >= self.limits.max_iterations:
                self.tripped = "LIMIT_ITERATIONS"
            elif self.tokens_used(event.agent) >= self.limits.max_tokens:
                self.tripped = "LIMIT_TOKENS"
            elif now - self.started >= self.limits.max_seconds:
                self.tripped = "LIMIT_TIME"
        if self.tripped is not None:
            event.cancel = f"Run limit reached ({self.tripped}); the harness escalates."
            return
        self.iterations += 1


@dataclass
class TerminationHook(HookProvider):
    ended: str | None = None
    ending_tool: str | None = None

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool)
        registry.add_callback(AfterToolsEvent, self.after_tools)

    def after_tool(self, event: AfterToolCallEvent) -> None:
        name = tool_name(event.tool_use["name"])
        if name not in END_REASONS or self.ended is not None:
            return
        payload = result_payload(event.result)
        if event.result.get("status") == "error" or "error" in payload:
            return
        if name == "propose_plan" and payload.get("accepted") is not True:
            return  # a rejected plan goes back to the model to be fixed
        self.ended = END_REASONS[name]
        self.ending_tool = name

    def after_tools(self, event: AfterToolsEvent) -> None:
        if self.ended is not None:
            event.end_turn = True
