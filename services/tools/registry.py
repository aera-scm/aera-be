"""The agent's tool catalogue (SRD 6.3.2): names, descriptions, input schemas, bindings.

One definition serves the AgentCore Gateway targets (each tool its own Lambda and read-only
role) and local runs. Deliberately absent: any tool that writes to SAP, sends a message or
changes configuration (confused-deputy defence). Tools of later milestones (simulate_plan,
get_supplier_reliability, request_supplier_info, request_replan) are not registered yet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from services.tools import calc, case_tools, sap_tools
from services.tools.context import ToolContext, ToolError


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: dict[str, dict[str, Any]]
    required: tuple[str, ...]
    call: Callable[..., dict[str, Any]]
    ends_run: bool = False

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": self.properties,
            "required": list(self.required),
            "additionalProperties": False,
        }

    def invoke(self, ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        unknown = set(arguments) - set(self.properties)
        if unknown:
            return {"error": f"unknown arguments: {', '.join(sorted(unknown))}"}
        missing = [name for name in self.required if arguments.get(name) in (None, "")]
        if missing:
            return {"error": f"missing arguments: {', '.join(missing)}"}
        kwargs = {_snake(name): value for name, value in arguments.items()}
        try:
            return self.call(ctx, **kwargs)
        except ToolError as error:
            return {"error": str(error)}


def _snake(name: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name)


S = {"type": "string"}
N = {"type": "number"}
TIME = {"type": "string", "description": "ISO 8601 date or time, UTC"}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "get_case_evidence",
        "Accepted signals of the case as tagged data, and extracted fields with their status.",
        {"caseId": S},
        ("caseId",),
        case_tools.get_case_evidence,
    ),
    ToolSpec(
        "sap_get_purchase_order",
        "Purchase order header, items and schedule lines from SAP, with sourceRefs.",
        {"poNumber": S},
        ("poNumber",),
        sap_tools.sap_get_purchase_order,
    ),
    ToolSpec(
        "sap_get_stock",
        "Unrestricted stock and consumption per hour for a material at a plant.",
        {"material": S, "plant": S},
        ("material", "plant"),
        sap_tools.sap_get_stock,
    ),
    ToolSpec(
        "sap_get_production_orders",
        "Production orders consuming the material at the plant in a time window.",
        {"material": S, "plant": S, "fromDate": TIME, "toDate": TIME},
        ("material", "plant", "fromDate", "toDate"),
        sap_tools.sap_get_production_orders,
    ),
    ToolSpec(
        "sap_get_sales_orders",
        "Sales order items pegged to the material (directly or through production), with "
        "net value and confirmed dates in the window.",
        {"material": S, "plant": S, "fromDate": TIME, "toDate": TIME},
        ("material", "plant", "fromDate", "toDate"),
        sap_tools.sap_get_sales_orders,
    ),
    ToolSpec(
        "sap_get_supplier",
        "Supplier name, compliance status and contact domains from SAP.",
        {"supplierId": S},
        ("supplierId",),
        sap_tools.sap_get_supplier,
    ),
    ToolSpec(
        "find_sources",
        "Other plants with stock free above minimum cover, and alternate suppliers.",
        {"material": S, "plant": S, "qtyNeeded": N, "needBy": TIME},
        ("material", "plant", "qtyNeeded", "needBy"),
        sap_tools.find_sources,
    ),
    ToolSpec(
        "calc_impact",
        "Deterministic impact: stock-out, units and orders at risk, revenue at risk. Give "
        "recoveryAt and its recoverySourceRef when evidence says when supply recovers.",
        {"caseId": S, "recoveryAt": TIME, "recoverySourceRef": S},
        ("caseId",),
        calc.calc_impact,
    ),
    ToolSpec(
        "calc_option",
        "Deterministic coverage, arrival and cost of one action from the rate card. "
        "actionType STO {fromPlant, qty}; AIR_FREIGHT {qtyFieldId | qty+qtySourceRef, "
        "remainderAt?, remainderSourceRef?}; ALTERNATE_SUPPLIER {supplierId, qty}.",
        {"caseId": S, "actionType": S, "params": {"type": "object"}},
        ("caseId", "actionType", "params"),
        calc.calc_option,
    ),
    ToolSpec(
        "ask_planner",
        "Ask the planner a question (e.g. to confirm an UNCONFIRMED field). Ends the run.",
        {"caseId": S, "question": S, "fieldId": S},
        ("caseId", "question"),
        case_tools.ask_planner,
        ends_run=True,
    ),
    ToolSpec(
        "propose_plan",
        "Submit the plan as Plan-schema JSON: options (2-3, each from calc_option results), "
        "chosen option ids and rationale. Ends the run when accepted.",
        {"caseId": S, "plan": {"type": "object"}},
        ("caseId", "plan"),
        case_tools.propose_plan,
        ends_run=True,
    ),
    ToolSpec(
        "escalate",
        "Hand the case to a human (Tier 3) with the reason. Ends the run.",
        {"caseId": S, "reason": S},
        ("caseId", "reason"),
        case_tools.escalate,
        ends_run=True,
    ),
)

BY_NAME = {tool.name: tool for tool in TOOLS}
ENDING = frozenset(tool.name for tool in TOOLS if tool.ends_run)
