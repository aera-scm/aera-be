"""A deterministic stand-in for the Bedrock model: plays scripted turns through Strands'
real agent loop, so hooks, tools and termination are exercised exactly as in production."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from dataclasses import dataclass
from typing import Any

from strands.models import Model

Messages = list[dict[str, Any]]


@dataclass
class Turn:
    thought: str
    tool: str | None = None
    arguments: Callable[[Messages], dict[str, Any]] | dict[str, Any] | None = None
    tokens: int = 1000


def last_result(messages: Messages, tool: str | None = None) -> dict[str, Any]:
    """The newest tool result (optionally of one tool) in the conversation."""
    names: dict[str, str] = {}
    for message in messages:
        for block in message.get("content", []):
            if "toolUse" in block:
                names[block["toolUse"]["toolUseId"]] = block["toolUse"]["name"]
    for message in reversed(messages):
        for block in message.get("content", []):
            result = block.get("toolResult")
            if not result or (tool and names.get(result["toolUseId"]) != tool):
                continue
            for content in result.get("content", []):
                if "json" in content:
                    return dict(content["json"])
                if "text" in content:
                    return dict(json.loads(content["text"]))
    return {}


class ScriptedModel(Model):
    def __init__(self, turns: list[Turn]) -> None:
        self.turns = list(turns)
        self.calls = 0
        self.seen: list[Messages] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> Any:
        return {}

    def structured_output(self, *args: Any, **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        raise NotImplementedError

    async def stream(  # type: ignore[override]
        self, messages: Messages, *args: Any, **kwargs: Any
    ) -> AsyncIterable[dict[str, Any]]:
        self.seen.append(messages)
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": turn.thought}}}
        yield {"contentBlockStop": {}}
        if turn.tool is not None:
            arguments = turn.arguments(messages) if callable(turn.arguments) else turn.arguments
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": f"t{self.calls}", "name": turn.tool}}
                }
            }
            yield {
                "contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(arguments or {})}}}
            }
            yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use" if turn.tool else "end_turn"}}
        yield {
            "metadata": {
                "usage": {
                    "inputTokens": turn.tokens,
                    "outputTokens": 10,
                    "totalTokens": turn.tokens + 10,
                },
                "metrics": {"latencyMs": 1},
            }
        }
