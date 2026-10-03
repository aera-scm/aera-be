"""EventBridge entrypoint for gated supplier reply matching."""

from typing import Any

from services.dialogue.replies import ReplyMatcher

_matcher: ReplyMatcher | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, bool]:
    global _matcher
    if _matcher is None:
        from services.shared import runtime

        _matcher = ReplyMatcher(runtime.client("dynamodb"), runtime.client("events"), runtime.env())
    signal_id = str((event.get("detail") or {}).get("data", {}).get("signalId") or "")
    return {"matched": _matcher.match(signal_id)}
