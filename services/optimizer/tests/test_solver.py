"""BR-20, FR-OPZ-02/05 and NFR-PERF-06: joint allocation, fail-closed status and speed."""

from datetime import UTC, datetime, timedelta
from time import perf_counter

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
