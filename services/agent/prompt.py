"""Prompt contract (SRD 6.3.3): versioned system prompt and the run's opening message.

The opening message carries the case record and the summaries of earlier runs of the same
case (SRD 6.18 "case context"); evidence itself only ever arrives through
`get_case_evidence`, inside Guardrails input tags.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from services.shared.models import Case

PROMPTS = Path(__file__).parent / "prompts"
PROMPT_VERSION = "supervisor_v1"


def system_prompt(version: str = PROMPT_VERSION) -> str:
    return (PROMPTS / f"{version}.md").read_text(encoding="utf-8")


def opening_message(
    case: Case, history: list[dict[str, Any]], *, mode: str, reason: str | None
) -> str:
    record = case.model_dump(
        mode="json",
        by_alias=True,
        include={
            "case_id",
            "type",
            "material",
            "material_description",
            "plant",
            "po_number",
            "po_item",
            "status",
            "rar_usd",
            "stockout_at",
            "priority_score",
            "days_late",
        },
    )
    earlier = [
        {
            "runId": run.get("runId"),
            "endReason": run.get("endReason"),
            "summary": run.get("summary"),
        }
        for run in history[-3:]
    ]
    return "\n".join(
        [
            f"Mode: {mode}. Reason for this run: {reason or 'new case'}.",
            "Case record (from AERA, SAP-derived):",
            json.dumps(record, indent=2),
            "Earlier runs of this case:",
            json.dumps(earlier, indent=2) if earlier else "none",
            f"Start by calling get_case_evidence with caseId {case.case_id}.",
        ]
    )
