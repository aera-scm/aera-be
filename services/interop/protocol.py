"""A2A and MCP adapters over the closed interop service operation set."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable
from typing import Any

from services.interop.logic import CLIENT_ID

Invoke = Callable[[str, str, dict[str, Any]], dict[str, Any]]
CASE_ID = re.compile(r"^EXC-[A-Za-z0-9-]{1,80}$")


def client_from_bearer(header: str, expected_client: str) -> str:
    """Read client identity from a JWT already verified by AgentCore Runtime."""
    if not header.startswith("Bearer ") or not expected_client or len(header) > 8192:
        raise ValueError("authenticated bearer token required")
    token = header[7:]
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("invalid JWT")
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise ValueError("invalid JWT claims") from error
    client = claims.get("client_id") if isinstance(claims, dict) else None
    if not isinstance(client, str) or client != expected_client or not CLIENT_ID.fullmatch(client):
        raise ValueError("OAuth client not allowed")
    return client


def card(url: str) -> dict[str, Any]:
    return {
        "name": "AERA exception intake",
        "description": (
            "Submit an exception signal and read case status. Control actions are unavailable."
        ),
        "url": url,
        "version": "1.0.0",
        "protocolVersion": "0.3.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": "submit_exception_signal",
                "name": "Submit exception signal",
                "tags": ["intake"],
            },
            {"id": "get_case_status", "name": "Get case status", "tags": ["read"]},
        ],
    }


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result(request_id: Any, value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _call(invoke: Invoke, client: str, operation: str, payload: dict[str, Any]) -> Any:
    response = invoke(client, operation, payload)
    status = int(response.get("statusCode", 500))
    body = response.get("body") or "{}"
    result = json.loads(body) if isinstance(body, str) else body
    if status >= 400:
        raise ValueError(f"service refused request ({status})")
    return result


def a2a(request: Any, client: str, invoke: Invoke) -> dict[str, Any]:
    if not isinstance(request, dict):
        return _error(None, -32600, "Invalid request")
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params")
    if request.get("jsonrpc") != "2.0" or not isinstance(params, dict):
        return _error(request_id, -32600, "Invalid request")
    try:
        if method == "message/send":
            message = params.get("message")
            if not isinstance(message, dict):
                raise ValueError("message required")
            parts = message.get("parts")
            if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
                raise ValueError("one structured signal part required")
            signal = parts[0].get("data")
            if not isinstance(signal, dict):
                raise ValueError("structured signal required")
            accepted = _call(invoke, client, "submit_exception_signal", signal)
            return _result(
                request_id,
                {
                    "kind": "task",
                    "id": accepted["signalId"],
                    "contextId": accepted["signalId"],
                    "status": {"state": "submitted"},
                    "metadata": {"gateStatus": accepted["status"]},
                },
            )
        if method == "tasks/get":
            task_id = params.get("id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("task id required")
            signal = _call(invoke, client, "get_signal_status", {"signalId": task_id})
            return _result(request_id, signal)
        if method == "get_case_status":
            case_id = params.get("caseId")
            if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id):
                raise ValueError("caseId required")
            return _result(request_id, _call(invoke, client, "get_case_status", params))
        return _error(request_id, -32601, "Method not found")
    except ValueError as error:
        return _error(request_id, -32602, str(error))


TOOLS = [
    {"name": "list_cases", "description": "List case summaries", "inputSchema": {"type": "object"}},
    {
        "name": "get_case_summary",
        "description": "Read one case summary",
        "inputSchema": {
            "type": "object",
            "properties": {"caseId": {"type": "string"}},
            "required": ["caseId"],
        },
    },
    {
        "name": "get_decision_record",
        "description": "Read verified decision record for a terminal case",
        "inputSchema": {
            "type": "object",
            "properties": {"caseId": {"type": "string"}},
            "required": ["caseId"],
        },
    },
]


def mcp(request: Any, client: str, invoke: Invoke) -> dict[str, Any]:
    if not isinstance(request, dict):
        return _error(None, -32600, "Invalid request")
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if request.get("jsonrpc") != "2.0" or not isinstance(params, dict):
        return _error(request_id, -32600, "Invalid request")
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "aera-readonly", "version": "1.0.0"},
            },
        )
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method != "tools/call":
        return _error(request_id, -32601, "Method not found")
    name = params.get("name")
    args = params.get("arguments") or {}
    operations = {
        "list_cases": "list_cases",
        "get_case_summary": "get_case_status",
        "get_decision_record": "get_decision_record",
    }
    if name not in operations or not isinstance(args, dict):
        return _error(request_id, -32602, "Tool unavailable")
    if name != "list_cases":
        case_id = args.get("caseId")
        if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id):
            return _error(request_id, -32602, "caseId required")
    try:
        value = _call(invoke, client, operations[name], args)
    except ValueError as error:
        return _result(
            request_id, {"content": [{"type": "text", "text": str(error)}], "isError": True}
        )
    return _result(request_id, {"content": [{"type": "text", "text": json.dumps(value)}]})
