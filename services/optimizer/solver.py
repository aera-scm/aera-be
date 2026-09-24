"""BR-20 / FR-OPZ-02: joint action sizing and allocation with CP-SAT.

Demand is split into dated buckets from the projection. A candidate can cover only buckets
it reaches on time. The solver prices action quantity plus uncovered revenue, subject to
shared stock, freight and supplier capacities. Its output is a candidate allocation, never
authority to write; every selected plan still needs V-01..V-15 and routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ortools.sat.python import cp_model

LIMIT_SECONDS = 5.0


@dataclass(frozen=True)
class Need:
    case_id: str
    id: str
    due: datetime
    quantity: int
    lost_revenue_cents_per_unit: int
    source_ref: str


@dataclass(frozen=True)
class Candidate:
    case_id: str
    id: str
    arrival: datetime
    max_quantity: int
    fixed_cost_cents: int
    unit_cost_cents: int
    resources: tuple[str, ...]
    source_ref: str
    sizeable: bool = True


@dataclass(frozen=True)
class Capacity:
    resource: str
    quantity: int
    source_ref: str


@dataclass(frozen=True)
class Allocation:
    candidate_id: str
    case_id: str
    quantity: int
    covered: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Result:
    status: str
    objective_cents: int | None
    allocations: tuple[Allocation, ...]
    uncovered: tuple[tuple[str, int], ...]
    requires_verification: bool = True


def _validate(needs: list[Need], candidates: list[Candidate], capacities: list[Capacity]) -> None:
    if not needs or not candidates:
        raise ValueError("portfolio requires needs and candidates")
    if len({n.id for n in needs}) != len(needs):
        raise ValueError("need ids must be unique")
    if len({a.id for a in candidates}) != len(candidates):
        raise ValueError("candidate ids must be unique")
    limits = {c.resource for c in capacities}
    if len(limits) != len(capacities):
        raise ValueError("resource capacities must be unique")
    cases = {n.case_id for n in needs}
    for need in needs:
        if (
            need.quantity <= 0
            or need.lost_revenue_cents_per_unit < 0
            or not need.source_ref
            or need.due.tzinfo is None
        ):
            raise ValueError("need quantity, value and source must be valid")
    for action in candidates:
        if (
            action.case_id not in cases
            or action.max_quantity <= 0
            or action.fixed_cost_cents < 0
            or action.unit_cost_cents < 0
            or not action.source_ref
            or action.arrival.tzinfo is None
            or not isinstance(action.sizeable, bool)
            or len(action.resources) != len(set(action.resources))
            or any(resource not in limits for resource in action.resources)
        ):
            raise ValueError("candidate quantity, cost, resource and source must be valid")
    if any(c.quantity < 0 or not c.source_ref for c in capacities):
        raise ValueError("capacity quantity and source must be valid")


def solve(
    needs: list[Need],
    candidates: list[Candidate],
    capacities: list[Capacity],
    *,
    limit_seconds: float = LIMIT_SECONDS,
) -> Result:
    _validate(needs, candidates, capacities)
    if not 0 < limit_seconds <= LIMIT_SECONDS:
        raise ValueError("solver time limit must be between 0 and 5 seconds")
    model = cp_model.CpModel()
    chosen = {a.id: model.new_bool_var(f"x_{a.id}") for a in candidates}
    quantity = {a.id: model.new_int_var(0, a.max_quantity, f"q_{a.id}") for a in candidates}
    for action in candidates:
        model.add(quantity[action.id] <= action.max_quantity * chosen[action.id])
        model.add(quantity[action.id] >= chosen[action.id])
        if not action.sizeable:
            model.add(quantity[action.id] == action.max_quantity * chosen[action.id])
    for capacity in capacities:
        model.add(
            sum(quantity[a.id] for a in candidates if capacity.resource in a.resources)
            <= capacity.quantity
        )
    covered: dict[tuple[str, str], cp_model.IntVar] = {}
    for action in candidates:
        eligible = [n for n in needs if n.case_id == action.case_id and action.arrival <= n.due]
        for need in eligible:
            covered[action.id, need.id] = model.new_int_var(
                0, min(action.max_quantity, need.quantity), f"cover_{action.id}_{need.id}"
            )
        model.add(sum(covered[action.id, n.id] for n in eligible) <= quantity[action.id])
    uncovered = {n.id: model.new_int_var(0, n.quantity, f"lost_{n.id}") for n in needs}
    for need in needs:
        model.add(
            uncovered[need.id]
            + sum(covered[a.id, need.id] for a in candidates if (a.id, need.id) in covered)
            == need.quantity
        )
    model.minimize(
        sum(
            a.fixed_cost_cents * chosen[a.id] + a.unit_cost_cents * quantity[a.id]
            for a in candidates
        )
        + sum(n.lost_revenue_cents_per_unit * uncovered[n.id] for n in needs)
    )
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = limit_seconds
    solver.parameters.num_search_workers = 8
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Result("INFEASIBLE" if status == cp_model.INFEASIBLE else "UNKNOWN", None, (), ())
    allocations = tuple(
        Allocation(
            a.id,
            a.case_id,
            solver.value(quantity[a.id]),
            tuple(
                (n.id, solver.value(covered[a.id, n.id]))
                for n in needs
                if (a.id, n.id) in covered and solver.value(covered[a.id, n.id]) > 0
            ),
        )
        for a in candidates
        if solver.value(chosen[a.id])
    )
    remaining = tuple((n.id, solver.value(uncovered[n.id])) for n in needs)
    result = Result(
        "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE",
        round(solver.objective_value),
        allocations,
        remaining,
    )
    _check_result(result, needs, candidates, capacities)
    return result


def _check_result(
    result: Result, needs: list[Need], candidates: list[Candidate], capacities: list[Capacity]
) -> None:
    """Fail closed if solver output violates a hard constraint or its own objective."""
    selected = {a.candidate_id: a for a in result.allocations}
    offered = {a.id: a for a in candidates}
    for allocation in result.allocations:
        action = offered[allocation.candidate_id]
        if not 0 < allocation.quantity <= action.max_quantity:
            raise ValueError("solver returned invalid action quantity")
        if sum(qty for _, qty in allocation.covered) > allocation.quantity:
            raise ValueError("solver counted coverage twice")
    for capacity in capacities:
        used = sum(
            a.quantity
            for a in result.allocations
            if capacity.resource in offered[a.candidate_id].resources
        )
        if used > capacity.quantity:
            raise ValueError("solver exceeded a shared capacity")
    remaining = dict(result.uncovered)
    for need in needs:
        covered = sum(dict(a.covered).get(need.id, 0) for a in result.allocations)
        if covered + remaining[need.id] != need.quantity:
            raise ValueError("solver lost or duplicated demand")
    objective = sum(
        offered[id].fixed_cost_cents + offered[id].unit_cost_cents * a.quantity
        for id, a in selected.items()
    ) + sum(n.lost_revenue_cents_per_unit * remaining[n.id] for n in needs)
    if objective != result.objective_cents:
        raise ValueError("solver objective does not match selected quantities")
