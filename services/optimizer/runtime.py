"""BR-20 at runtime: joint allocation of competing cases (FR-OPZ-01..05, SRD 6.11, ADR-0021).

The Verifier calls `PortfolioService.solve_for(case)` before it checks a plan:

1. Every open case with a plan that is not yet executed (PLAN_PROPOSED, VERIFIED,
   AWAITING_APPROVAL) contributes its options as candidate actions (FR-OPZ-03).
2. `portfolio.detect` groups the cases that share a finite resource (FR-OPZ-01). A case
   without a competitor has no portfolio and nothing below runs.
3. Needs come from each case's recorded impact; lost revenue per unit is weighted by the
   priority of the customers whose sales orders are at risk (`PRIO#` entries, default 1).
4. Capacities, all sourced, never invented (ADR-0021): donor stock above minimum cover and
   unreserved in the ledger (BR-07, BR-08); freight per supplier and receiving plant from
   `FREIGHT#` entries; supplier partials from the case's usable `QUANTITY` field. A candidate
   whose resource has no sourced capacity is left out and listed as excluded.
5. CP-SAT (in its own Lambda container) solves jointly and, as the baseline, each case
   alone in the order its plan was proposed. The saving is the difference (FR-OPZ-04).
6. The result is stored on every member case (`PORTFOLIO`) and announced as
   `PortfolioSolved`. It is never authority to write: V-15 and routing still apply.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from services.execution.ledger import Ledger
from services.optimizer.portfolio import Portfolio, PortfolioCase, detect
from services.rules.br_02 import usable
from services.shared.dynamo import from_item, table_name, to_item
from services.shared.models import Case, CaseStatus, Option, PlanRecord, ProposedPlan
from services.shared.runtime import emit
from services.shared.triage import SALES, odata_quote
from services.tools.case_tools import ACTION_OF, constraint_breaches
from services.tools.context import ToolContext
from services.tools.sap_tools import component_requirements, stock_position

COMPONENT = "optimizer"
COMPETING = (CaseStatus.PLAN_PROPOSED, CaseStatus.VERIFIED, CaseStatus.AWAITING_APPROVAL)
CENTS = Decimal(100)
DEFAULT_PRIORITY = Decimal(1)

# (needs, candidates, capacities) in the optimizer Lambda's JSON shape -> its result.
Solver = Callable[[dict[str, Any]], dict[str, Any]]


def _cents(value: Decimal) -> int:
    return int((value * CENTS).to_integral_value(rounding=ROUND_FLOOR))


def _whole(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def candidate_id(case_id: str, option_id: str) -> str:
    return f"{case_id}/{option_id}"


@dataclass
class Inputs:
    needs: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    capacities: dict[str, dict[str, Any]] = field(default_factory=dict)
    excluded: list[dict[str, str]] = field(default_factory=list)
    partials: set[tuple[str, str]] = field(default_factory=set)

    def event(self, case_ids: set[str] | None = None) -> dict[str, Any]:
        needy = {n["caseId"] for n in self.needs}

        def keep(row: dict[str, Any]) -> bool:
            return (case_ids is None or row["caseId"] in case_ids) and row["caseId"] in needy

        return {
            "needs": [n for n in self.needs if keep(n)],
            "candidates": [c for c in self.candidates if keep(c)],
            "capacities": list(self.capacities.values()),
        }


@dataclass
class PortfolioService:
    ctx: ToolContext
    solver: Solver
    minimum_cover_days: Decimal

    def __post_init__(self) -> None:
        self._cases = table_name("cases", self.ctx.env)
        self._config = table_name("config", self.ctx.env)
        self.ledger = Ledger(self.ctx.dynamodb, self.ctx.env)

    # Reads ------------------------------------------------------------------------------

    def _item(self, case_id: str, sk: str) -> dict[str, Any] | None:
        item = self.ctx.dynamodb.get_item(
            TableName=self._cases,
            Key={"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": sk}},
            ConsistentRead=True,
        ).get("Item")
        return from_item(item) if item else None

    def _plan(self, case: Case) -> PlanRecord | None:
        if not case.plan_version:
            return None
        item = self._item(case.case_id, f"PLAN#{case.plan_version}")
        return PlanRecord.from_stored(item) if item else None

    def open_cases(self) -> list[tuple[Case, PlanRecord]]:
        found: list[tuple[Case, PlanRecord]] = []
        for status in COMPETING:
            arguments: dict[str, Any] = {
                "TableName": self._cases,
                "IndexName": "GSI1",
                "KeyConditionExpression": "#s = :s",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {":s": {"S": status.value}},
            }
            while True:
                page = self.ctx.dynamodb.query(**arguments)
                for item in page.get("Items", []):
                    if item.get("SK", {}).get("S") != "META":
                        continue
                    case = self.ctx.cases.get(item["caseId"]["S"])
                    record = self._plan(case) if case else None
                    if case is not None and record is not None:
                        found.append((case, record))
                if "LastEvaluatedKey" not in page:
                    break
                arguments["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return sorted(found, key=lambda pair: pair[0].case_id)

    def committed(self, case: Case) -> set[str]:
        """Options of parts already auto-approved (Tier 1) or approved: their stock is held in
        the ledger and so already left out of donor capacity; they are not candidates again."""
        route = self._item(case.case_id, f"ROUTE#{case.plan_version}")
        taken: set[str] = set()
        for part in (route or {}).get("parts") or []:
            record = self._item(case.case_id, f"PART#{part['id']}") or {}
            if int(part.get("tier") or 0) == 1 or record.get("decision") == "APPROVED":
                taken.update(str(option) for option in part.get("options") or [])
        return taken

    def _config_item(self, key: str) -> dict[str, Any] | None:
        item = self.ctx.dynamodb.get_item(
            TableName=self._config, Key={"PK": {"S": key}}, ConsistentRead=True
        ).get("Item")
        return from_item(item) if item else None

    def priority(self, customer: str) -> tuple[Decimal, str | None]:
        entry = self._config_item(f"PRIO#{customer}")
        if entry is None:
            return DEFAULT_PRIORITY, None
        return Decimal(str(entry["weight"])), f"config:PRIO#{customer}"

    def freight(self, supplier: str, plant: str, on: datetime) -> dict[str, Any] | None:
        key = f"FREIGHT#{supplier}#{plant}"
        entry = self._config_item(key)
        if entry is None:
            return None
        day = on.date().isoformat()
        if (
            not str(entry.get("validFrom", "0001-01-01"))
            <= day
            <= str(entry.get("validTo", "9999-12-31"))
        ):
            return None
        return {
            "resource": key,
            "quantity": _whole(Decimal(str(entry["qtyPerDay"]))),
            "sourceRef": f"config:{key}",
        }

    def partial(self, case: Case) -> tuple[Decimal, str] | None:
        """The supplier's confirmed ready partial: the case's usable QUANTITY field."""
        for signal in self.ctx.signals.for_case(case.case_id):
            for found in signal.fields:
                if str(found.name) == "QUANTITY" and usable(found):
                    try:
                        qty = Decimal(str(found.value).replace(",", "").split()[0])
                    except (ArithmeticError, IndexError, ValueError):
                        continue
                    ref = (
                        f"planner:{found.confirmed_by}"
                        if found.confirmed_by
                        else f"signal:{signal.signal_id}/{found.field_id}"
                    )
                    return qty, ref
        return None

    def donor_capacity(self, material: str, plant: str) -> dict[str, Any]:
        """BR-07 and BR-08: what a donor may give without breaking its own cover."""
        position = stock_position(self.ctx, material, plant)
        per_hour = position["consumptionPerHour"] or Decimal(0)
        horizon = self.ctx.now().timestamp() + float(self.minimum_cover_days) * 86400
        committed = sum(
            (
                r["openQuantity"]
                for r in component_requirements(self.ctx, material, plant)
                if r["requiredAt"].timestamp() <= horizon
            ),
            Decimal(0),
        )
        on_hand = position["unrestricted"]
        keep = max(per_hour * 24 * self.minimum_cover_days, committed)
        held = self.ledger.allocated(material=material, plant=plant)
        free = max(Decimal(0), min(on_hand - keep, on_hand - held))
        refs = position["stockSourceRefs"][:1] or [f"SAP:stock/{material}/{plant}"]
        return {
            "resource": f"DONOR#{material}#{plant}",
            "quantity": _whole(free),
            "sourceRef": refs[0],
        }

    def customer(self, sales_order: str) -> str | None:
        rows = self.ctx.sap.query(
            SALES,
            "A_SalesOrder",
            filter=f"SalesOrder eq {odata_quote(sales_order)}",
            run_id=self.ctx.run_id,
        )
        return str(rows[0].data.get("SoldToParty") or "") or None if rows else None

    # Inputs ------------------------------------------------------------------------------

    def needs(self, case: Case) -> list[dict[str, Any]]:
        impact = self._item(case.case_id, "IMPACT")
        if not impact:
            return []
        units = Decimal(str(impact.get("unitsAtRisk") or 0))
        if units <= 0:
            return []
        weighted = Decimal(0)
        for exposure in impact.get("salesOrdersAtRisk") or []:
            customer = self.customer(str(exposure["salesOrder"]))
            weight, _ = self.priority(customer) if customer else (DEFAULT_PRIORITY, None)
            weighted += Decimal(str(exposure["netUsd"])) * weight
        if weighted <= 0:
            weighted = Decimal(str(impact.get("rarUsd") or 0))
        per_unit = _cents(weighted / units)
        rows = []
        for order in impact.get("productionOrdersAtRisk") or []:
            short = _whole(Decimal(str(order["unitsShort"])))
            if short <= 0:
                continue
            rows.append(
                {
                    "caseId": case.case_id,
                    "id": f"{case.case_id}/{order['productionOrder']}",
                    "due": str(order["requiredAt"]),
                    "quantity": short,
                    "lostRevenueCentsPerUnit": per_unit,
                    "sourceRef": str(order["sourceRef"]),
                }
            )
        return rows

    def _resources(self, case: Case, option: Option, inputs: Inputs) -> list[str] | None:
        keys: list[str] = []
        for action in option.actions:
            if action.type == "CREATE_STO":
                key = f"DONOR#{action.material}#{action.from_plant}"
                if key not in inputs.capacities:
                    inputs.capacities[key] = self.donor_capacity(action.material, action.from_plant)
            elif action.type == "BOOK_AIR_FREIGHT":
                freight = self.freight(action.supplier_id, case.plant, option.arrival)
                partial = self.partial(case)
                if freight is None or partial is None:
                    return None
                inputs.capacities.setdefault(freight["resource"], freight)
                key = f"PARTIAL#{action.supplier_id}#{case.material}"
                current = inputs.capacities.get(key)
                if (key, case.case_id) not in inputs.partials:
                    # Each case's confirmed partial adds to what the supplier has ready.
                    inputs.partials.add((key, case.case_id))
                    inputs.capacities[key] = {
                        "resource": key,
                        "quantity": _whole(partial[0]) + (current["quantity"] if current else 0),
                        "sourceRef": current["sourceRef"] if current else partial[1],
                    }
                keys.append(freight["resource"])
            elif action.type == "CREATE_PO_ALTERNATE":
                return None  # no sourced partial capacity for a new supplier (ADR-0021)
            else:
                continue
            if key not in keys:
                keys.append(key)
        return keys

    def inputs(self, members: list[tuple[Case, PlanRecord]]) -> Inputs:
        inputs = Inputs()
        rates = {rate.source_ref: rate for rate in self.ctx.rates.entries()}
        for case, record in members:
            inputs.needs.extend(self.needs(case))
            taken = self.committed(case)
            limits = self._item(case.case_id, "CONSTRAINTS") or {}
            for option in record.plan.options:
                if not within_constraints(option, limits):
                    inputs.excluded.append(
                        {
                            "caseId": case.case_id,
                            "optionId": option.id,
                            "reason": "outside the planner's constraints (FR-CHT-01)",
                        }
                    )
                    continue
                if option.id in taken:
                    inputs.excluded.append(
                        {
                            "caseId": case.case_id,
                            "optionId": option.id,
                            "reason": "already approved; its stock is held (BR-08)",
                        }
                    )
                    continue
                resources = self._resources(case, option, inputs)
                rate = rates.get(option.cost_source_ref)
                if resources is None or rate is None:
                    inputs.excluded.append(
                        {
                            "caseId": case.case_id,
                            "optionId": option.id,
                            "reason": "no sourced capacity or rate (ADR-0021)",
                        }
                    )
                    continue
                inputs.candidates.append(
                    {
                        "caseId": case.case_id,
                        "id": candidate_id(case.case_id, option.id),
                        "arrival": option.arrival.isoformat(),
                        "maxQuantity": _whole(option.coverage_units),
                        "fixedCostCents": _cents(rate.fixed_cost_usd),
                        "unitCostCents": _cents(rate.unit_cost_usd),
                        "resources": resources,
                        "sourceRef": option.cost_source_ref,
                        "sizeable": all(a.type == "CREATE_STO" for a in option.actions),
                    }
                )
        return inputs

    # Solve -------------------------------------------------------------------------------

    def _alone(self, inputs: Inputs, order: list[str]) -> int | None:
        """Baseline: each case solved on its own, first proposed first served."""
        left = {key: dict(value) for key, value in inputs.capacities.items()}
        total = 0
        for case_id in order:
            event = inputs.event({case_id})
            if not event["needs"]:
                continue
            if not event["candidates"]:
                total += sum(n["quantity"] * n["lostRevenueCentsPerUnit"] for n in event["needs"])
                continue
            event["capacities"] = list(left.values())
            result = self.solver(event)
            if result.get("objectiveCents") is None:
                return None
            total += int(result["objectiveCents"])
            used = {c["id"]: c["resources"] for c in event["candidates"]}
            for allocation in result["allocations"]:
                for key in used[allocation["candidateId"]]:
                    left[key]["quantity"] -= int(allocation["quantity"])
        return total

    def solve_for(self, case_id: str) -> dict[str, Any] | None:
        members = self.open_cases()
        entries = [PortfolioCase(case, tuple(record.plan.options)) for case, record in members]
        now = self.ctx.now()
        portfolio: Portfolio | None = next(
            (p for p in detect(entries, now) if case_id in p.case_ids), None
        )
        if portfolio is None:
            return None
        chosen = [(c, r) for c, r in members if c.case_id in portfolio.case_ids]
        inputs = self.inputs(chosen)
        digest = hashlib.sha256("|".join(portfolio.case_ids).encode()).hexdigest()
        portfolio_id = f"PF-{digest[:12]}"
        record: dict[str, Any] = {
            "portfolioId": portfolio_id,
            "caseIds": list(portfolio.case_ids),
            "sharedResources": list(portfolio.shared_resources),
            "solvedAt": now.isoformat(),
            "capacities": list(inputs.capacities.values()),
            "excluded": inputs.excluded,
            "candidateActions": inputs.candidates,
        }
        if not inputs.needs or not inputs.candidates:
            record.update({"solverStatus": "INFEASIBLE", "objective": None, "allocations": []})
        else:
            joint = self.solver(inputs.event())
            order = [c.case_id for c, r in sorted(chosen, key=lambda pair: pair[1].proposed_at)]
            alone = self._alone(inputs, order)
            objective = joint.get("objectiveCents")
            record.update(
                {
                    "solverStatus": joint["status"]
                    if joint["status"] in ("OPTIMAL", "FEASIBLE", "INFEASIBLE")
                    else "TIMEOUT",
                    "objective": Decimal(objective) / CENTS if objective is not None else None,
                    "singleObjective": Decimal(alone) / CENTS if alone is not None else None,
                    "savingVsSingle": (
                        Decimal(alone - objective) / CENTS
                        if alone is not None and objective is not None
                        else None
                    ),
                    "allocations": joint["allocations"],
                    "uncovered": joint.get("uncovered", []),
                }
            )
        record["inputsHash"] = hashlib.sha256(
            json.dumps(inputs.event(), sort_keys=True, default=str).encode()
        ).hexdigest()
        for member in portfolio.case_ids:
            self.ctx.dynamodb.put_item(
                TableName=self._cases,
                Item=to_item({"PK": f"CASE#{member}", "SK": "PORTFOLIO", **record}),
            )
        emit(
            self.ctx.bus,
            "PortfolioSolved",
            {
                "portfolioId": portfolio_id,
                "caseIds": list(portfolio.case_ids),
                "solverStatus": record["solverStatus"],
            },
            component=COMPONENT,
            case_id=case_id,
            environment=self.ctx.env,
        )
        return record


def within_constraints(option: Option, limits: dict[str, Any]) -> bool:
    """FR-CHT-01: an option the planner ruled out is never offered to the solver."""
    if "maxCostUsd" in limits and option.cost_usd > Decimal(str(limits["maxCostUsd"])):
        return False
    excluded = {x for x in str(limits.get("excludedActions") or "").split(",") if x}
    if excluded and any(ACTION_OF.get(x) in {a.type for a in option.actions} for x in excluded):
        return False
    if "needBy" in limits and option.arrival.date() > date.fromisoformat(str(limits["needBy"])):
        return False
    return True


def allocation_for(record: dict[str, Any] | None, case_id: str) -> dict[str, Decimal] | None:
    """V-15 input: option id -> allocated quantity for this case; None without a portfolio
    or when the solver found no feasible allocation (the case is then escalated, FR-OPZ-05)."""
    if record is None:
        return None
    prefix = f"{case_id}/"
    return {
        str(a["candidateId"])[len(prefix) :]: Decimal(str(a["quantity"]))
        for a in record.get("allocations") or []
        if str(a["candidateId"]).startswith(prefix)
    }


def lambda_solver(client: Any, function_name: str) -> Solver:
    def solve(event: dict[str, Any]) -> dict[str, Any]:
        response = client.invoke(FunctionName=function_name, Payload=json.dumps(event).encode())
        if response.get("FunctionError"):
            return {"status": "UNKNOWN", "objectiveCents": None, "allocations": []}
        result: dict[str, Any] = json.loads(response["Payload"].read())
        return result

    return solve


def donor_allocation(record: dict[str, Any] | None, case_id: str) -> dict[str, Decimal] | None:
    """Units of each donor (`DONOR#material#plant`) the portfolio gave this case."""
    if record is None:
        return None
    resources = {str(c["id"]): c["resources"] for c in record.get("candidateActions") or []}
    given: dict[str, Decimal] = {}
    for allocation in record.get("allocations") or []:
        if str(allocation.get("caseId")) != case_id:
            continue
        for key in resources.get(str(allocation["candidateId"]), []):
            if str(key).startswith("DONOR#"):
                given[str(key)] = given.get(str(key), Decimal(0)) + Decimal(
                    str(allocation["quantity"])
                )
    return given


def conforms(record: PlanRecord, allocation: dict[str, Decimal]) -> bool:
    chosen = {o.id: o for o in record.plan.options if o.id in record.plan.chosen}
    return set(chosen) == set(allocation) and all(
        chosen[oid].coverage_units == qty for oid, qty in allocation.items()
    )


def resized_plan(
    ctx: ToolContext, case: Case, record: PlanRecord, allocation: dict[str, Decimal]
) -> ProposedPlan | None:
    """FR-OPZ-03: the solver only selects and sizes the case's own priced options. A transfer
    is re-priced at its allocated quantity through `calc_option`; other options are taken
    whole or not at all. None when nothing is allocated (the case gave everything up)."""
    from services.tools.calc import calc_option

    if not allocation:
        return None
    options: list[Option] = []
    sentences: list[str] = []
    for option in record.plan.options:
        qty = allocation.get(option.id)
        if qty is not None and qty != option.coverage_units:
            sto = next((a for a in option.actions if a.type == "CREATE_STO"), None)
            if sto is None or len(option.actions) != 1:
                return None
            draft = calc_option(ctx, case.case_id, "STO", {"fromPlant": sto.from_plant, "qty": qty})
            option = Option.model_validate(
                {
                    "id": option.id,
                    "name": option.name,
                    "actions": draft["actions"],
                    "coverageUnits": draft["coverageUnits"],
                    "arrival": draft["arrival"],
                    "costUsd": draft["costUsd"],
                    "costSourceRef": draft["costSourceRef"],
                    "figures": draft["figures"],
                    "rationale": (
                        f"Option {option.id} transfers {draft['coverageUnits']} PC from plant "
                        f"{sto.from_plant}, arriving {draft['arrival']}, cost USD "
                        f"{draft['costUsd']} ({draft['costSourceRef']})."
                    ),
                }
            )
        if qty is not None:
            sentences.append(option.rationale)
        options.append(option)
    chosen = sorted(allocation)
    selected = [o for o in options if o.id in chosen]
    revised = ProposedPlan.model_validate(
        {
            "caseId": case.case_id,
            "planVersion": record.plan.plan_version + 1,
            "options": [o.model_dump(mode="json", by_alias=True) for o in options],
            "chosen": chosen,
            "totalCostUsd": sum((o.cost_usd for o in selected), Decimal(0)),
            "coverageUnits": sum((o.coverage_units for o in selected), Decimal(0)),
            "rationale": " ".join(sentences),
        }
    )
    # FR-CHT-01: the solver's sizing must still respect the planner's constraints.
    return None if constraint_breaches(ctx, case.case_id, revised) else revised
