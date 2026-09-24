"""Run-starter (SRD 6.17, 6.18, NFR-REL-04): async, one active run per case."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from services.conftest import RecordingBus
from services.run_starter.handler import RunStarter
from services.shared.cases import CaseStore
from services.shared.models import Case, CaseStatus

ENV = "test"
T0 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
ARN = "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/aera_dev_agent-abc"


class FakeAgentCore:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def invoke_agent_runtime(self, **request: Any) -> dict[str, Any]:
        if self.fail:
            raise ConnectionError("unreachable")
        self.calls.append(request)
        return {"statusCode": 200}


@pytest.fixture
def case(dynamodb: Any) -> str:
    store = CaseStore(dynamodb, ENV)
    store.create(
        Case(
            case_id="EXC-2026-0914",
            type="MRP_EXCEPTION",
            material="MAT-48219",
            plant="1010",
            status=CaseStatus.RECEIVED,
            created_at=T0,
            updated_at=T0,
        ),
        actor="system",
    )
    store.transition("EXC-2026-0914", CaseStatus.TRIAGED, actor="system")
    return "EXC-2026-0914"


def starter(dynamodb: Any, bus: RecordingBus, agentcore: FakeAgentCore) -> RunStarter:
    return RunStarter(
        dynamodb=dynamodb, agentcore=agentcore, runtime_arn=lambda: ARN, bus=bus, env=ENV
    )


def test_nfr_rel_04_claims_the_case_and_invokes_the_runtime_asynchronously(
    dynamodb: Any, bus: RecordingBus, case: str
) -> None:
    agentcore = FakeAgentCore()

    started = starter(dynamodb, bus, agentcore).start(case, reason="opened")

    assert started.run_id is not None
    [call] = agentcore.calls
    assert call["agentRuntimeArn"] == ARN
    assert call["runtimeSessionId"] == f"{case}-{started.run_id}"
    assert len(call["runtimeSessionId"]) >= 33  # AgentCore's minimum session id length
    assert json.loads(call["payload"]) == {
        "mode": "investigate",
        "caseId": case,
        "runId": started.run_id,
        "reason": "opened",
    }
    stored = CaseStore(dynamodb, ENV).get(case)
    assert stored is not None and stored.status is CaseStatus.INVESTIGATING
    assert stored.active_run_id == started.run_id
    assert bus.types() == ["RunStarted"]


def test_one_active_run_per_case(dynamodb: Any, bus: RecordingBus, case: str) -> None:
    agentcore = FakeAgentCore()
    service = starter(dynamodb, bus, agentcore)
    service.start(case, reason="opened")

    again = service.start(case, reason="duplicate event")

    assert again.run_id is None
    assert len(agentcore.calls) == 1


def test_unreachable_runtime_releases_the_case_and_reports_the_failure(
    dynamodb: Any, bus: RecordingBus, case: str
) -> None:
    started = starter(dynamodb, bus, FakeAgentCore(fail=True)).start(case, reason="opened")

    assert started.run_id is None and started.reason == "runtime not reachable"
    stored = CaseStore(dynamodb, ENV).get(case)
    assert stored is not None and stored.active_run_id is None
    assert bus.details("RunEnded")[0]["data"]["endReason"] == "LIMIT_ERROR"


def test_cases_in_other_states_are_not_started(dynamodb: Any, bus: RecordingBus) -> None:
    assert starter(dynamodb, bus, FakeAgentCore()).start("EXC-2026-9999", reason="x").run_id is None
