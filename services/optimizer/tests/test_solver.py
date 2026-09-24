"""BR-20, FR-OPZ-02/05 and NFR-PERF-06: joint allocation, fail-closed status and speed."""

from datetime import UTC, datetime, timedelta
from time import perf_counter

import pytest

from services.optimizer.handler import lambda_handler
from services.optimizer.solver import Candidate, Capacity, Need, solve

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)


def need(case: str, qty: int, value: int) -> Need:
    return Need(case, f"{case}-order", NOW + timedelta(hours=8), qty, value, "SAP:ORDER/QTY")


def action(case: str, qty: int, resource: str, *, arrival_hours: int = 5) -> Candidate:
    return Candidate(
        case,
        f"{case}-sto",
        NOW + timedelta(hours=arrival_hours),
        qty,
        0,
        100,
        (resource,),
        "ratecard:STO-1",
    )


def test_at_18_br_20_joint_allocation_beats_first_case_greedy() -> None:
    demands = [need("high", 600, 10_000), need("low", 600, 5_000)]
    candidates = [action("high", 600, "DONOR#MAT-A#1020"), action("low", 600, "DONOR#MAT-A#1020")]
    capacity = [Capacity("DONOR#MAT-A#1020", 600, "SAP:STOCK/QTY")]

    result = solve(demands, candidates, capacity)

    assert result.status == "OPTIMAL" and result.requires_verification
    assert [(a.case_id, a.quantity) for a in result.allocations] == [("high", 600)]
    assert dict(result.uncovered) == {"high-order": 0, "low-order": 600}
    # Greedy arrival-order allocation to the low-value case costs 6.06M cents.
    assert result.objective_cents == 3_060_000 < 6_060_000


def test_fr_opz_02_sizes_an_action_and_rejects_a_late_candidate() -> None:
    demands = [need("one", 400, 1_000)]
    candidates = [
        action("one", 600, "DONOR#MAT-A#1020"),
        Candidate("one", "late", NOW + timedelta(hours=9), 400, 0, 1, (), "ratecard:AIR-1"),
    ]

    result = solve(demands, candidates, [Capacity("DONOR#MAT-A#1020", 600, "SAP:STOCK/QTY")])

    assert [(a.candidate_id, a.quantity, a.covered) for a in result.allocations] == [
        ("one-sto", 400, (("one-order", 400),))
    ]
    assert result.objective_cents == 40_000


def test_br_20_selects_only_one_freight_mode_per_shipment() -> None:
    demands = [need("one", 100, 1_000), need("two", 100, 900)]
    candidates = [
        Candidate(
            "one", "air", NOW + timedelta(hours=2), 100, 0, 10,
            (), "ratecard:AIR", exclusive_group="PO-1#10",
        ),
        Candidate(
            "two", "road", NOW + timedelta(hours=3), 100, 0, 10,
            (), "ratecard:ROAD", exclusive_group="PO-1#10",
        ),
    ]

    result = solve(demands, candidates, [])

    assert [(a.candidate_id, a.quantity) for a in result.allocations] == [("air", 100)]
    assert result.objective_cents == 91_000


def test_nfr_perf_06_thirty_cases_two_hundred_candidates_under_five_seconds() -> None:
    demands = [need(f"case-{i:02d}", 100, 1_000 + i) for i in range(30)]
    candidates = [
        Candidate(
            f"case-{i % 30:02d}",
            f"candidate-{i:03d}",
            NOW + timedelta(hours=2 + i % 5),
            100,
            0,
            10 + i % 7,
            (f"DONOR#{i % 5}",),
            "ratecard:TEST-1",
        )
        for i in range(200)
    ]
    capacities = [Capacity(f"DONOR#{i}", 600, "SAP:STOCK/QTY") for i in range(5)]

    start = perf_counter()
    result = solve(demands, candidates, capacities)
    elapsed = perf_counter() - start

    assert result.status in {"OPTIMAL", "FEASIBLE"}
    assert elapsed < 5, f"solver took {elapsed:.3f} s"
    assert sum(qty for _, qty in result.uncovered) == 0


def test_internal_lambda_returns_only_a_candidate_allocation() -> None:
    event = {
        "needs": [
            {
                "caseId": "case-1",
                "id": "order-1",
                "due": "2026-10-05T16:00:00Z",
                "quantity": 100,
                "lostRevenueCentsPerUnit": 1000,
                "sourceRef": "SAP:ORDER/QTY",
            }
        ],
        "candidates": [
            {
                "caseId": "case-1",
                "id": "sto-1",
                "arrival": "2026-10-05T13:00:00Z",
                "maxQuantity": 100,
                "fixedCostCents": 0,
                "unitCostCents": 10,
                "resources": ["DONOR#MAT-A#1020"],
                "sourceRef": "ratecard:STO-1",
            }
        ],
        "capacities": [
            {"resource": "DONOR#MAT-A#1020", "quantity": 100, "sourceRef": "SAP:STOCK/QTY"}
        ],
    }

    result = lambda_handler(event, None)

    assert result["status"] == "OPTIMAL"
    assert result["allocations"][0]["quantity"] == 100
    assert result["requiresVerification"] is True
    with pytest.raises(ValueError, match="JSON integers"):
        lambda_handler(
            {**event, "capacities": [{**event["capacities"][0], "quantity": 99.5}]}, None
        )
