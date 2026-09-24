"""Offline stand-ins for Bedrock Guardrails, Textract and Comprehend, shared by the
pipeline tests and the evaluation runner (WP-3, WP-13)."""

from typing import Any

from generate_signals import ATTACK


class Guard:
    """ApplyGuardrail stand-in: flags the prompt-attack sentence used by the data set."""

    def apply_guardrail(self, **request: Any) -> dict[str, Any]:
        text = request["content"][0]["text"]["text"]
        if ATTACK.lower()[:40] in text.lower() or "approve air freight now" in text.lower():
            return {
                "action": "GUARDRAIL_INTERVENED",
                "assessments": [
                    {
                        "contentPolicy": {
                            "filters": [{"type": "PROMPT_ATTACK", "confidence": "HIGH"}]
                        }
                    }
                ],
            }
        return {"action": "NONE"}


class Ocr:
    """Textract stand-in: the reference photo's quantity comes back at 71% confidence."""

    def analyze_document(self, **request: Any) -> dict[str, Any]:
        key = request["Document"]["S3Object"]["Name"]
        if "/whatsapp/" not in key:
            return {"Blocks": []}
        return {
            "Blocks": [
                {
                    "Id": "q",
                    "BlockType": "QUERY",
                    "Query": {"Alias": "QUANTITY"},
                    "Relationships": [{"Type": "ANSWER", "Ids": ["a"]}],
                },
                {"Id": "a", "BlockType": "QUERY_RESULT", "Text": "640", "Confidence": 71.0},
            ]
        }


class Language:
    def detect_dominant_language(self, Text: str) -> dict[str, Any]:
        return {"Languages": [{"LanguageCode": "en", "Score": 0.99}]}
