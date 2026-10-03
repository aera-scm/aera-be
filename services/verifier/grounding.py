"""Bedrock contextual grounding adapter (FR-VER-03); unavailable scores fail closed.

ADR-0033 (owner decision, 2026-10-03): G in BR-18 is the mean of the GROUNDING scores of the
rationale's sentences, each checked against the same source. Bedrock scores a whole text far
below its sentences (live: 0.36 whole, 0.69 mean, 0.72-0.88 for factual sentences), so the
whole-text score left every live plan under the BR-05 floor. RELEVANCE answers "does this
address the case" and is taken from the whole rationale; a single sentence is not an answer.
If any call fails, the whole check is unavailable.
"""

import logging
import re
from decimal import Decimal
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from services.verifier.logic import Grounding

LOG = logging.getLogger(__name__)
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])")
# Bounds the calls per verification; anything beyond is scored as one last chunk.
MAX_SENTENCES = 20


def sentences(text: str) -> list[str]:
    parts = [part.strip() for part in SENTENCE.split(text.strip()) if part.strip()]
    if len(parts) > MAX_SENTENCES:
        parts = [*parts[: MAX_SENTENCES - 1], " ".join(parts[MAX_SENTENCES - 1 :])]
    return parts


def _scores(
    client: Any, guardrail_id: str, version: str, source: str, query: str, text: str
) -> dict[str, Decimal]:
    response = client.apply_guardrail(
        guardrailIdentifier=guardrail_id,
        guardrailVersion=version,
        source="OUTPUT",
        content=[
            {"text": {"text": source, "qualifiers": ["grounding_source"]}},
            {"text": {"text": query, "qualifiers": ["query"]}},
            {"text": {"text": text, "qualifiers": ["guard_content"]}},
        ],
    )
    scores: dict[str, list[Decimal]] = {}
    for assessment in response.get("assessments", []):
        for item in assessment.get("contextualGroundingPolicy", {}).get("filters", []):
            scores.setdefault(item["type"], []).append(Decimal(str(item["score"])))
    return {"GROUNDING": min(scores["GROUNDING"]), "RELEVANCE": min(scores["RELEVANCE"])}


def evaluate(
    client: Any, guardrail_id: str, version: str, *, rationale: str, source: str, query: str
) -> Grounding:
    if not all((guardrail_id, version, rationale.strip(), source.strip(), query.strip())):
        return Grounding()
    try:
        whole = _scores(client, guardrail_id, version, source, query, rationale)
        parts = [
            _scores(client, guardrail_id, version, source, query, part)["GROUNDING"]
            for part in sentences(rationale)
        ]
        score = sum(parts, Decimal(0)) / len(parts)
        result = Grounding(score, whole["RELEVANCE"], True)
        return result if result.valid() else Grounding()
    except KeyError:
        # The guardrail answered without grounding scores: it has no grounding policy.
        LOG.warning("Grounding check returned no grounding and relevance scores")
        return Grounding()
    except ClientError as error:
        LOG.warning("Grounding check unavailable: %s", error.response.get("Error", {}).get("Code"))
        return Grounding()
    except (BotoCoreError, TypeError, ValueError, ArithmeticError, ZeroDivisionError) as error:
        LOG.warning("Grounding check unavailable: %s", type(error).__name__)
        return Grounding()
