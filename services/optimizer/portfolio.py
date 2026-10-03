"""FR-OPZ-01: group open cases whose candidate actions share a finite resource."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from services.shared.models import Case, CaseStatus, Option

HORIZON = timedelta(days=30)
CLOSED = frozenset(
    {
        CaseStatus.CLOSED,
        CaseStatus.ROLLED_BACK,
        CaseStatus.FAILED_ROLLED_BACK,
    }
)


@dataclass(frozen=True)
class PortfolioCase:
    case: Case
    candidates: tuple[Option, ...]


@dataclass(frozen=True)
class Portfolio:
    case_ids: tuple[str, ...]
    shared_resources: tuple[str, ...]


def resources(entry: PortfolioCase, now: datetime) -> frozenset[str]:
    """Resource keys come from case/SAP action fields, never option prose."""
    case = entry.case
    keys = {f"STOCK#{case.material}#{case.plant}"}
    for option in entry.candidates:
        if not now <= option.arrival <= now + HORIZON:
            continue
        for action in option.actions:
            if action.type == "CREATE_STO":
                keys.add(f"DONOR#{action.material}#{action.from_plant}")
            elif action.type == "BOOK_AIR_FREIGHT":
                keys.add(f"FREIGHT#{action.supplier_id}#{case.plant}")
                keys.add(f"PARTIAL#{action.supplier_id}#{case.material}")
            elif action.type == "CREATE_PO_ALTERNATE":
                keys.add(f"PARTIAL#{action.supplier_id}#{case.material}")
    return frozenset(keys)


def detect(entries: list[PortfolioCase], now: datetime) -> list[Portfolio]:
    """Connected components of shared resources; single cases are not portfolios."""
    active = sorted(
        (e for e in entries if e.case.status not in CLOSED), key=lambda e: e.case.case_id
    )
    claims = {e.case.case_id: resources(e, now) for e in active}
    neighbours: dict[str, set[str]] = {e.case.case_id: set() for e in active}
    shared: dict[frozenset[str], set[str]] = {}
    for index, left in enumerate(active):
        for right in active[index + 1 :]:
            overlap = claims[left.case.case_id] & claims[right.case.case_id]
            if not overlap:
                continue
            a, b = left.case.case_id, right.case.case_id
            neighbours[a].add(b)
            neighbours[b].add(a)
            shared[frozenset({a, b})] = set(overlap)
    result = []
    unseen = set(neighbours)
    while unseen:
        root = min(unseen)
        component = {root}
        pending = [root]
        unseen.remove(root)
        while pending:
            node = pending.pop()
            for neighbour in sorted(neighbours[node] & unseen):
                unseen.remove(neighbour)
                component.add(neighbour)
                pending.append(neighbour)
        if len(component) > 1:
            common = set().union(
                *(resources for pair, resources in shared.items() if pair <= component)
            )
            result.append(Portfolio(tuple(sorted(component)), tuple(sorted(common))))
    return sorted(result, key=lambda p: p.case_ids)
