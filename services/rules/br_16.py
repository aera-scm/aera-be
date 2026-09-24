"""BR-16 / FR-CHT-03: chat cannot authorise. Reading a planner's chat message for what it
asks; the decisions about it are made in code, never by the model.

A message may ask to execute or approve (refused: only an approver or Tier 1 routing can),
to change a tier, threshold or allowlist (refused: admin configuration only), and may state
re-planning constraints (budget, date, excluded actions) that start a constrained re-plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

_EXECUTE = re.compile(
    r"\b(execute|execution|run it|do it|go ahead|approve|approved|push it|book it|"
    r"skip (the )?approval|without approval|place the order)\b",
    re.IGNORECASE,
)
_GOVERNANCE = re.compile(
    r"\b(threshold|tier|autonomy|approval limit|allow ?list|white ?list|kill ?switch|"
    r"auto-?approv\w*|guardrail|recipient)\b",
    re.IGNORECASE,
)
_CHANGE = re.compile(
    r"\b(raise|increase|lower|change|set|disable|turn off|remove|add|bypass)\b", re.I
)
_BUDGET = re.compile(
    r"\b(?:under|below|max(?:imum)?|within|at most|less than|up to)\s*(?:usd|\$)?\s*"
    r"([\d][\d,]*(?:\.\d+)?)\s*(k)?\b",
    re.IGNORECASE,
)
_DATE = re.compile(r"\b(?:by|before|no later than)\s+(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)
_EXCLUDE = {
    "AIR_FREIGHT": re.compile(r"\b(no|without|avoid)\s+air(\s*freight)?\b", re.I),
    "ALTERNATE_SUPPLIER": re.compile(r"\b(no|without|avoid)\s+(alternate|other)\s+supplier", re.I),
    "STO": re.compile(r"\b(no|without|avoid)\s+(sto|stock transfer|transfer)\b", re.I),
}


@dataclass(frozen=True)
class ChatIntent:
    execute: bool = False
    governance_change: bool = False
    max_cost_usd: Decimal | None = None
    need_by: date | None = None
    excluded: tuple[str, ...] = ()
    constraints: dict[str, str] = field(default_factory=dict)

    @property
    def replan(self) -> bool:
        return bool(self.constraints)


def read(message: str) -> ChatIntent:
    budget = _BUDGET.search(message)
    max_cost = None
    if budget:
        max_cost = Decimal(budget.group(1).replace(",", ""))
        if budget.group(2):
            max_cost *= 1000
    when = _DATE.search(message)
    need_by = None
    if when:
        try:
            need_by = date.fromisoformat(when.group(1))
        except ValueError:
            need_by = None
    excluded = tuple(action for action, pattern in _EXCLUDE.items() if pattern.search(message))
    constraints: dict[str, str] = {}
    if max_cost is not None:
        constraints["maxCostUsd"] = str(max_cost)
    if need_by is not None:
        constraints["needBy"] = need_by.isoformat()
    if excluded:
        constraints["excludedActions"] = ",".join(excluded)
    return ChatIntent(
        execute=bool(_EXECUTE.search(message)),
        governance_change=bool(_GOVERNANCE.search(message) and _CHANGE.search(message)),
        max_cost_usd=max_cost,
        need_by=need_by,
        excluded=excluded,
        constraints=constraints,
    )
