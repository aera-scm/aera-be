"""AgentCore Runtime HTTP entry point for external A2A and read-only MCP calls."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import boto3

from services.interop.protocol import a2a, card, client_from_bearer, mcp

MAX_BODY = 64 * 1024


def invoke_api(client_id: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = os.environ["AERA_INTEROP_API_FUNCTION"]
    request = {
        "serviceContext": {"clientId": client_id},
        "operation": operation,
        "payload": payload,
    }
    response = boto3.client("lambda").invoke(
        FunctionName=name,
        InvocationType="RequestResponse",
        Payload=json.dumps(request).encode(),
    )
    if response.get("FunctionError"):
        raise RuntimeError("service API failed")
    result = json.loads(response["Payload"].read())
    if not isinstance(result, dict) or "statusCode" not in result:
        raise RuntimeError("service API returned an invalid response")
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "AERAInterop/1"

    def _send(self, status: int, value: Any) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client(self) -> str | None:
        try:
            return client_from_bearer(
                self.headers.get("Authorization", ""), os.environ["AERA_INTEROP_CLIENT_ID"]
            )
        except (KeyError, ValueError):
            self._send(401, {"error": "authenticated OAuth client required"})
            return None

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/ping":
            self._send(200, {"status": "Healthy"})
            return
        if self._client() is None:
            return
        if self.path == "/.well-known/agent-card.json":
            url = os.environ.get("AERA_AGENT_CARD_URL", "")
            if not url:
                self._send(503, {"error": "agent card URL not configured"})
            else:
                self._send(200, card(url))
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        client = self._client()
        if client is None:
            return
        size = self.headers.get("Content-Length", "")
        if not size.isdecimal() or not 0 < int(size) <= MAX_BODY:
            self._send(413, {"error": "bounded JSON body required"})
            return
        try:
            request = json.loads(self.rfile.read(int(size)))
        except (ValueError, UnicodeError):
            self._send(400, {"error": "invalid JSON"})
            return
        mode = os.environ.get("AERA_INTEROP_PROTOCOL", "")
        if self.path in ("/", "/invocations") and mode == "A2A":
            self._send(200, a2a(request, client, invoke_api))
        elif self.path in ("/mcp", "/invocations") and mode == "MCP":
            self._send(200, mcp(request, client, invoke_api))
        else:
            self._send(404, {"error": "not found"})


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
