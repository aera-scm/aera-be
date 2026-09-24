"""Daily refresh of 180-day supplier performance."""

from typing import Any

from services.dialogue.reliability_job import ReliabilityJob

_job: ReliabilityJob | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    global _job
    if _job is None:
        from services.shared import runtime

        _job = ReliabilityJob(runtime.sap_client(), runtime.client("dynamodb"), runtime.env())
    return {"profiles": _job.refresh()}
