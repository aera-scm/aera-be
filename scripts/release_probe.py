"""Live API smoke and optional agent warm-up for the final runbook (SRD 9.2/9.4)."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

READY = {
    "VERIFIED",
    "AWAITING_APPROVAL",
    "AUTO_APPROVED",
    "APPROVED",
    "EXECUTING",
    "MONITORING",
    "CLOSED",
    "ESCALATED",
}


def probe(
    client: httpx.Client,
    base_url: str,
    token: str,
    *,
    case_id: str | None = None,
    timeout_seconds: float = 120,
    now: Callable[[], float] = time.monotonic,
    pause: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if not base_url.startswith("https://") or not token:
        raise ValueError("HTTPS API URL and access token are required")
    base = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}

    def request(method: str, path: str) -> Any:
        response = client.request(method, f"{base}{path}", headers=headers)
        response.raise_for_status()
        return response.json()

    board = request("GET", "/cases")
    metrics = request("GET", "/metrics")
    if not isinstance(board, list) or not isinstance(metrics, dict):
        raise ValueError("API smoke returned invalid case or metrics payload")
    result: dict[str, Any] = {"caseCount": len(board), "metricsReady": "kpis" in metrics}
    if not result["metricsReady"]:
        raise ValueError("KPI payload missing")
    if case_id is None:
        return result
    initial = request("GET", f"/cases/{case_id}")
    if not isinstance(initial, dict) or initial.get("case", {}).get("caseId") != case_id:
        raise ValueError("warm-up case not found")
    started = request("POST", f"/cases/{case_id}/runs")
    if not isinstance(started, dict) or not started.get("runId"):
        raise ValueError("agent run did not return a run ID")
    result["runId"] = started["runId"]
    deadline = now() + timeout_seconds
    while now() < deadline:
        detail = request("GET", f"/cases/{case_id}")
        status = detail.get("case", {}).get("status") if isinstance(detail, dict) else None
        if status in READY:
            result["caseId"] = case_id
            result["status"] = status
            return result
        pause(2)
    raise TimeoutError(f"warm-up case {case_id} did not reach a verified or escalated state")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True, help="HTTPS API stage URL")
    parser.add_argument("--warmup-case", help="Case ID that may safely run through the agent")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    token = os.environ.get("AERA_PROBE_ACCESS_TOKEN", "")
    with httpx.Client(timeout=30.0) as client:
        result = probe(
            client,
            args.api_url,
            token,
            case_id=args.warmup_case,
            timeout_seconds=args.timeout,
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
