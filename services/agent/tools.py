"""How the supervisor gets its tools (SRD 6.18).

- Deployed: from the AgentCore Gateway as MCP tools, requests signed with the runtime's
  IAM identity (SigV4, service `bedrock-agentcore`).
- Local (`make agent-local`, tests): the same tool implementations called in process.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
from strands.tools.tools import PythonAgentTool
from strands.types.tools import ToolResult

from services.tools.context import ToolContext
from services.tools.registry import TOOLS, ToolSpec


def _local(spec: ToolSpec, ctx: ToolContext) -> PythonAgentTool:
    def run(*args: Any, **_: Any) -> ToolResult:
        tool_use = args[0]
        result = spec.invoke(ctx, dict(tool_use.get("input") or {}))
        return {
            "toolUseId": tool_use["toolUseId"],
            "status": "error" if "error" in result else "success",
            "content": [{"json": result}],
        }

    return PythonAgentTool(
        spec.name,
        {
            "name": spec.name,
            "description": spec.description,
            "inputSchema": {"json": spec.input_schema()},
        },
        run,
    )


def local_tools(ctx: ToolContext) -> list[PythonAgentTool]:
    return [_local(spec, ctx) for spec in TOOLS]


class SigV4Auth(httpx.Auth):
    """Signs each MCP request to the Gateway with the runtime role's credentials."""

    requires_request_body = True

    def __init__(self, region: str, service: str = "bedrock-agentcore") -> None:
        import boto3

        credentials = boto3.Session().get_credentials()
        if credentials is None:
            raise RuntimeError("no AWS credentials for signing Gateway requests")
        self._credentials = credentials
        self._region = region
        self._service = service

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        from botocore.auth import SigV4Auth as Signer
        from botocore.awsrequest import AWSRequest

        signed = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={k: v for k, v in request.headers.items() if k.lower() != "connection"},
        )
        Signer(self._credentials.get_frozen_credentials(), self._service, self._region).add_auth(
            signed
        )
        request.headers.update(dict(signed.headers))
        yield request


def gateway_client(url: str, region: str) -> Any:
    from strands.tools.mcp import MCPClient

    return MCPClient(url=url, auth_provider=SigV4Auth(region))
