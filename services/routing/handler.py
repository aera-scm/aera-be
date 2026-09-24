"""Approval timer (BR-23): a one-shot schedule per pending part invokes this at the
reminder time and at the deadline; the durable part record decides what happens."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from services.routing.store import ControlStore

_store: ControlStore | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _store
    if _store is None:
        from services.shared import runtime

        _store = ControlStore(runtime.client("dynamodb"), runtime.env())
    case_id, part_id = str(event["caseId"]), str(event["planPartId"])
    _store.tick(case_id, part_id, now=datetime.now(UTC))
    part = _store.get(case_id, f"PART#{part_id}") or {}
    return {"reminded": bool(part.get("reminded")), "expired": bool(part.get("expired"))}
