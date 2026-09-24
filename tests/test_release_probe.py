"""Final runbook smoke fails when API or agent warm-up is not ready."""

import httpx
import pytest
from release_probe import probe


def test_release_probe_checks_kpis_and_agent_progress() -> None:
    seen: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        if request.url.path == "/dev/cases":
            return httpx.Response(200, json=[])
        if request.url.path == "/dev/metrics":
            return httpx.Response(200, json={"kpis": {}})
        if request.method == "POST":
            return httpx.Response(202, json={"runId": "run-1"})
        return httpx.Response(
            200,
            json={"case": {"caseId": "EXC-2026-0914", "status": "VERIFIED"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    result = probe(client, "https://api.example/dev", "synthetic-token", case_id="EXC-2026-0914")
    assert result == {
        "caseCount": 0,
        "metricsReady": True,
        "runId": "run-1",
        "caseId": "EXC-2026-0914",
        "status": "VERIFIED",
    }
    assert ("POST", "/dev/cases/EXC-2026-0914/runs") in seen


def test_release_probe_rejects_missing_kpis() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dev/cases":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    with pytest.raises(ValueError, match="KPI payload missing"):
        probe(httpx.Client(transport=httpx.MockTransport(respond)), "https://api.example/dev", "x")
