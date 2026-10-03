"""Bedrock contextual grounding adapter (FR-VER-03); unavailable scores fail closed."""

import logging
from decimal import Decimal
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from services.verifier.logic import Grounding

LOG = logging.getLogger(__name__)


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
    except KeyError:
        # The guardrail answered without grounding scores: it has no grounding policy.
        LOG.warning("Grounding check returned no grounding and relevance scores")
        return Grounding()
    except ClientError as error:
        LOG.warning("Grounding check unavailable: %s", error.response.get("Error", {}).get("Code"))
        return Grounding()
    except (BotoCoreError, TypeError, ValueError, ArithmeticError) as error:
        LOG.warning("Grounding check unavailable: %s", type(error).__name__)
        return Grounding()
