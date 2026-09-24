"""WP-9 held-out synthetic portfolios with independently tabulated optima."""

from datetime import UTC, datetime, timedelta

import pytest

from services.optimizer.solver import Candidate, Capacity, Need, solve

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)

# id, shared resource, revenue/unit, action cost/unit, capacity, late actions,
# selected case IDs, total cost in cents. Each case needs 100 units.
PORTFOLIOS = [
    ("donor-01", "DONOR", (100, 50), (10, 10), 100, (), (0,), 6_000),
    ("donor-02", "DONOR", (80, 120), (10, 20), 100, (), (1,), 10_000),
    ("donor-03", "DONOR", (70, 65), (60, 5), 100, (), (1,), 7_500),
    ("donor-04", "DONOR", (30, 20), (40, 5), 100, (), (1,), 3_500),
    ("donor-05", "DONOR", (100, 90), (10, 20), 200, (), (0, 1), 3_000),
    ("freight-01", "FREIGHT", (100, 50), (20, 10), 100, (), (0,), 7_000),
    ("freight-02", "FREIGHT", (50, 80), (5, 5), 100, (), (1,), 5_500),
    ("freight-03", "FREIGHT", (25, 30), (30, 40), 100, (), (), 5_500),
    ("freight-04", "FREIGHT", (80, 60), (15, 5), 200, (), (0, 1), 2_000),
    ("freight-05", "FREIGHT", (200, 50), (10, 10), 100, (0,), (1,), 21_000),
    ("partial-01", "PARTIAL", (60, 70), (5, 5), 100, (), (1,), 6_500),
    ("partial-02", "PARTIAL", (100, 80), (30, 5), 100, (), (1,), 10_500),
    ("partial-03", "PARTIAL", (100, 80), (30, 20), 100, (), (0,), 11_000),
    ("partial-04", "PARTIAL", (100, 80), (30, 20), 200, (), (0, 1), 5_000),
    ("partial-05", "PARTIAL", (100, 80), (30, 20), 100, (1,), (0,), 11_000),
    ("stock-01", "STOCK", (20, 30), (1, 1), 100, (), (1,), 2_100),
    ("stock-02", "STOCK", (50, 50), (10, 0), 100, (), (1,), 5_000),
    ("stock-03", "STOCK", (50, 50), (10, 0), 200, (), (0, 1), 1_000),
    ("stock-04", "STOCK", (50, 50), (10, 0), 100, (0, 1), (), 10_000),
    ("stock-05", "STOCK", (40, 100), (1, 99), 100, (), (0,), 10_100),
]


@pytest.mark.parametrize("scenario", PORTFOLIOS, ids=[row[0] for row in PORTFOLIOS])
def test_br_20_known_portfolio_optimum(scenario: tuple[object, ...]) -> None:
    name, resource, values, costs, capacity, late, selected, objective = scenario
    assert isinstance(name, str) and isinstance(resource, str)
    assert isinstance(values, tuple) and isinstance(costs, tuple)
    assert isinstance(capacity, int) and isinstance(late, tuple)
    assert isinstance(selected, tuple) and isinstance(objective, int)
    needs = [
        Need(f"{name}-{i}", f"need-{i}", NOW + timedelta(hours=8), 100, value, "SAP:ORDER")
        for i, value in enumerate(values)
    ]
    actions = [
        Candidate(
            f"{name}-{i}",
            f"action-{i}",
            NOW + timedelta(hours=9 if i in late else 5),
            100,
            0,
            cost,
            (resource,),
            "ratecard:TEST",
        )
        for i, cost in enumerate(costs)
    ]

    result = solve(needs, actions, [Capacity(resource, capacity, "SAP:CAPACITY")])

    assert result.status == "OPTIMAL"
    assert tuple(int(a.candidate_id[-1]) for a in result.allocations) == selected
    assert all(a.quantity == 100 for a in result.allocations)
    assert result.objective_cents == objective
