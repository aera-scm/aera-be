"""Bedrock contextual grounding adapter (FR-VER-03); unavailable scores fail closed."""

from decimal import Decimal
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from services.verifier.logic import Grounding


def evaluate(
    client: Any, guardrail_id: str, version: str, *, rationale: str, source: str, query: str
) -> Grounding:
    if not all((guardrail_id, version, rationale.strip(), source.strip(), query.strip())):
        return Grounding()
    try:
        response = client.apply_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=version,
            source="OUTPUT",
            content=[
                {"text": {"text": source, "qualifiers": ["grounding_source"]}},
                {"text": {"text": query, "qualifiers": ["query"]}},
                {"text": {"text": rationale, "qualifiers": ["guard_content"]}},
            ],
        )
        scores: dict[str, list[Decimal]] = {}
        for assessment in response.get("assessments", []):
            for item in assessment.get("contextualGroundingPolicy", {}).get("filters", []):
                scores.setdefault(item["type"], []).append(Decimal(str(item["score"])))
        result = Grounding(min(scores["GROUNDING"]), min(scores["RELEVANCE"]), True)
        return result if result.valid() else Grounding()
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError, ArithmeticError):
        return Grounding()
