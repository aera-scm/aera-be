"""AgentCore Gateway Lambda target: one deployed function per tool (SRD 6.3.2, 6.18).

The function's `AERA_TOOL_NAME` fixes which tool it serves, so each tool keeps its own
role. The Gateway passes the tool arguments as the event and names the tool in the client
context as `<target>___<tool>`; a mismatch is refused rather than routed elsewhere.
"""

from __future__ import annotations

import os
from typing import Any

from services.tools.context import ToolContext
from services.tools.registry import BY_NAME

_context: ToolContext | None = None


def requested_tool(context: Any) -> str | None:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    name = custom.get("bedrockAgentCoreToolName")
    return str(name).split("___")[-1] if name else None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _context
    served = os.environ["AERA_TOOL_NAME"]
    asked = requested_tool(context)
    if asked is not None and asked != served:
        return {"error": f"this target serves {served}, not {asked}"}
    if _context is None:
        from services.shared import runtime

        _context = ToolContext(
            sap=runtime.sap_client(),
            dynamodb=runtime.client("dynamodb"),
            bus=runtime.client("events"),
        )
    return BY_NAME[served].invoke(_context, dict(event or {}))
