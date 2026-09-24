"""WP-13: the 25-case regression subset runs on every change, and a regression fails the
build (SRD 8.3, 8.4; OBJ-06). A negative control proves the scoring can fail."""

from collections.abc import Iterator

import pytest
from mirror_process import AVAILABLE, MISSING, running_mirror
from runner import T0, load_cases, metrics, portfolio_results, report, run_all, run_case

pytestmark = pytest.mark.skipif(not AVAILABLE, reason=MISSING)


@pytest.fixture(scope="module")
def mirror() -> Iterator[str]:
    with running_mirror(T0) as url:
        yield url


def test_the_ci_subset_has_25_cases_across_the_categories() -> None:
    cases = load_cases("ci")
    assert len(cases) == 25
    assert {c.category for c in cases} >= {
        "LATE_PO_CLEAR",
        "LATE_PO_CONFLICT",
        "MATERIAL_SHORTAGE",
        "CARRIER_DELAY",
        "LOW_CONFIDENCE",
        "ADVERSARIAL",
        "NO_VIABLE_OPTION",
    }


def test_obj_06_ci_subset_meets_the_deterministic_targets(mirror: str) -> None:
    results = run_all(load_cases("ci"), mirror)
    markdown, _ = report(results, "ci")

    values = metrics(results)
    assert values["casesPassed"] == (25, 25), markdown
    for name in ("figureAccuracy", "tierAccuracy", "adversarialContainment"):
        passed, total = values[name]
        assert total > 0 and passed == total, (name, markdown)


def test_a_wrong_ground_truth_is_reported_as_a_failure(mirror: str) -> None:
    [case] = [c for c in load_cases("ci") if c.id == "lpc-01"]
    case.truth.tier = 1
    case.truth.figures["unitsAtRisk"] = case.truth.figures["unitsAtRisk"] + 1

    from datetime import datetime

    result = run_case(case, mirror, datetime.fromisoformat(T0.replace("Z", "+00:00")))

    assert not result.passed
    assert sorted(c.name for c in result.checks if not c.ok) == ["tier", "unitsAtRisk"]


def test_br_20_full_report_scores_all_known_portfolios() -> None:
    portfolios = portfolio_results()
    markdown, data = report([], None, portfolios)

    assert len(portfolios) == 20
    assert all(p["passed"] for p in portfolios), markdown
    assert len(data["portfolios"]) == 20
    assert "Optimiser quality | 100.0% (20/20)" in markdown


def test_fr_lng_01_full_set_has_grounded_german_and_indonesian_cases(mirror: str) -> None:
    cases = load_cases("ml")
    assert len(cases) == 15
    assert {case.multilingual_language for case in cases} == {"de", "id"}

    results = run_all(cases, mirror)
    markdown, _ = report(results, "ml")
    assert all(result.passed for result in results), markdown
    assert metrics(results)["extractionAccuracy"] == (30, 30)
