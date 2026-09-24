"""FR-INT / BR-21: protocol operations cannot become control operations."""

import base64
import json
from typing import Any

import pytest

from services.interop.protocol import a2a, card, client_from_bearer, mcp


def token(client: str) -> str:
    claims = (
        base64.urlsafe_b64encode(json.dumps({"client_id": client}).encode()).decode().rstrip("=")
    )
    return f"Bearer e30.{claims}.signature"


def test_fr_int_03_requires_runtime_verified_client_identity() -> None:
    assert client_from_bearer(token("aera-client"), "aera-client") == "aera-client"
    for header in ("", token("other-client"), "Bearer bad"):
        with pytest.raises(ValueError):
            client_from_bearer(header, "aera-client")
    numeric = base64.urlsafe_b64encode(b'{"client_id": 123}').decode().rstrip("=")
    with pytest.raises(ValueError):
        client_from_bearer(f"Bearer e30.{numeric}.signature", "aera-client")


def test_fr_int_01_a2a_submits_signal_and_reads_task_status() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def invoke(client: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((client, operation, payload))
        value = (
            {"signalId": "01KTEST123456789ABCDEFGHJK", "status": "SUBMITTED_TO_GATE"}
            if operation == "submit_exception_signal"
            else {"id": "01KTEST123456789ABCDEFGHJK", "status": {"state": "accepted"}}
        )
        return {"statusCode": 202, "body": json.dumps(value)}

    assert {skill["id"] for skill in card("https://example.invalid/invocations")["skills"]} == {
        "submit_exception_signal",
        "get_case_status",
    }
    submitted = a2a(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {"message": {"parts": [{"data": {"messageId": "m-1", "text": "late PO"}}]}},
        },
        "aera-client",
        invoke,
    )
    assert submitted["result"]["status"]["state"] == "submitted"
    status = a2a(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tasks/get",
            "params": {"id": submitted["result"]["id"]},
        },
        "aera-client",
        invoke,
    )
    assert status["result"]["status"]["state"] == "accepted"
    assert [call[1] for call in calls] == ["submit_exception_signal", "get_signal_status"]


def test_br_21_a2a_and_mcp_refuse_control_methods() -> None:
    def forbidden(*args: Any) -> dict[str, Any]:
        raise AssertionError("must not reach service API")

    denied = a2a(
        {"jsonrpc": "2.0", "id": 1, "method": "approve_plan", "params": {}},
        "aera-client",
        forbidden,
    )
    assert denied["error"]["code"] == -32601
    listed = mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "aera-client", forbidden)
    assert listed is not None
    assert {tool["name"] for tool in listed["result"]["tools"]} == {
        "list_cases",
        "get_case_summary",
        "get_decision_record",
    }
    denied_tool = mcp(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "execute"}},
        "aera-client",
        forbidden,
    )
    assert denied_tool is not None
    assert denied_tool["error"]["code"] == -32602


def test_fr_int_02_mcp_calls_only_read_service_operation() -> None:
    calls: list[str] = []

    def invoke(client: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(operation)
        return {"statusCode": 200, "body": json.dumps({"caseId": payload.get("caseId")})}

    result = mcp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_case_summary", "arguments": {"caseId": "EXC-2026-0914"}},
        },
        "aera-client",
        invoke,
    )
    assert result is not None
    assert json.loads(result["result"]["content"][0]["text"]) == {"caseId": "EXC-2026-0914"}
    assert calls == ["get_case_status"]


def test_fr_int_02_mcp_initialization_notification_has_no_response() -> None:
    def forbidden(*args: Any) -> dict[str, Any]:
        raise AssertionError("notification must not reach service API")

    assert (
        mcp(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            "aera-client",
            forbidden,
        )
        is None
    )
