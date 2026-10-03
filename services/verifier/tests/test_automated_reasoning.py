"""FR-VER-04 / AT-24: policy findings can only escalate deterministic routing."""

import logging
from datetime import timedelta
from typing import Any

from botocore.exceptions import ClientError

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

    assert statements.endswith(f"The proposed autonomy tier is {proposed.tier}.")
    assert "Total chosen action cost" in statements
    assert "Verifier confidence" in statements
    assert "reversible" in statements or "irreversible" in statements
    assert "Revenue at risk protected by the chosen plan" in statements
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


def test_fr_ver_04_facts_are_premises_and_the_tier_is_the_claim() -> None:
    """ADR-0030: the live policy translates only its own variables; facts go as the query and
    the tier as the guarded content, and option or donor detail lines are not sent."""
    result = verified()
    bedrock = Bedrock("satisfiable")

    assessment = assess(bedrock, "id", "1", "arn", result, Route(2, result.version_hash))

    content: list[dict[str, Any]] = bedrock.requests[0]["content"]  # type: ignore[assignment]
    premises, claim = content
    assert premises["text"]["qualifiers"] == ["query"]
    assert claim["text"] == {
        "text": "The proposed autonomy tier is 2.",
        "qualifiers": ["guard_content"],
    }
    assert len(premises["text"]["text"].splitlines()) == 7
    assert (
        "Option" not in premises["text"]["text"] and "Donor plant" not in premises["text"]["text"]
    )
    assert assessment.status == "CONSISTENT" and not assessment.disagrees


def test_fr_ver_04_too_complex_or_untranslated_is_not_agreement() -> None:
    result = verified()
    for finding in ("tooComplex", "noTranslations", "translationAmbiguous"):
        assessment = assess(
            Bedrock(finding), "id", "1", "arn", result, Route(2, result.version_hash)
        )
        assert assessment.status == "AMBIGUOUS" and assessment.disagrees


def test_fr_ver_04_service_errors_are_logged_without_content(caplog: Any) -> None:
    class Denied:
        def apply_guardrail(self, **request: object) -> dict[str, object]:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "ApplyGuardrail"
            )

    result = verified()
    with caplog.at_level(logging.WARNING):
        assessment = assess(Denied(), "id", "1", "arn", result, Route(2, result.version_hash))

    assert assessment.status == "UNAVAILABLE" and assessment.disagrees
    assert "AccessDeniedException" in caplog.text and "US cents" not in caplog.text
