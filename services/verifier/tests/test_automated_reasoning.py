"""FR-VER-04 / AT-24: policy findings can only escalate deterministic routing."""

from datetime import timedelta

from services.routing.logic import Route, route
from services.verifier.automated_reasoning import assess, decisive_statements
from services.verifier.tests.test_verification import NOW, limits, verified


class Bedrock:
    def __init__(self, finding: str) -> None:
        self.finding = finding
        self.requests: list[dict[str, object]] = []

    def apply_guardrail(self, **request: object) -> dict[str, object]:
        self.requests.append(request)
        return {
            "assessments": [
                {
                    "automatedReasoningPolicy": {
                        "findings": [{self.finding: {}}],
                    }
                }
            ]
        }


def test_fr_ver_04_renders_decisive_facts_from_plan_and_evidence() -> None:
    result = verified()
    proposed = route(
        result,
        now=NOW,
        stockout=NOW + timedelta(hours=24),
        plant="1010",
        limits=limits(),
    )
    statements = decisive_statements(result, proposed)

    assert "proposed autonomy tier" in statements
    assert "Total chosen action cost" in statements
    assert "Verifier confidence" in statements
    assert "reversible" in statements or "irreversible" in statements
    assert "revenue at risk" in statements
    assert "Donor cover and customer commitments check V-07" in statements


def test_at_24_invalid_policy_finding_escalates_with_both_results() -> None:
    result = verified()
    proposed = Route(1, result.version_hash)
    bedrock = Bedrock("invalid")

    assessment = assess(bedrock, "guardrail-id", "1", "policy-arn", result, proposed)

    assert assessment.status == "INVALID" and assessment.disagrees
    assert assessment.findings == ("INVALID",)
    assert assessment.policy_arn == "policy-arn"
    assert bedrock.requests[0]["outputScope"] == "FULL"


def test_fr_ver_04_valid_policy_reports_only_and_missing_policy_fails_closed() -> None:
    result = verified()
    proposed = Route(2, result.version_hash)
    valid = assess(Bedrock("valid"), "guardrail-id", "1", "policy-arn", result, proposed)
    missing = assess(Bedrock("valid"), "", "", None, result, proposed)

    assert valid.status == "VALID" and not valid.disagrees
    assert missing.status == "UNAVAILABLE" and missing.disagrees


def test_fr_ver_04_unrecognised_finding_cannot_be_treated_as_valid() -> None:
    result = verified()
    assessment = assess(
        Bedrock("futureFinding"), "id", "1", "arn", result, Route(2, result.version_hash)
    )
    assert assessment.status == "AMBIGUOUS" and assessment.disagrees
