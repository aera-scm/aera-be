"""API Gateway (REST, proxy integration) request and response helpers (SRD 6.10).

Errors are RFC 7807 problem+json. Header lookups ignore case, as HTTP does.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any


def header(event: dict[str, Any], name: str) -> str | None:
    wanted = name.lower()
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == wanted:
            return str(value)
    return None


def body_bytes(event: dict[str, Any]) -> bytes:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return str(body).encode()


def query(event: dict[str, Any], name: str) -> str | None:
    value = (event.get("queryStringParameters") or {}).get(name)
    return None if value is None else str(value)


def response(
    status: int, body: Any = None, *, content_type: str = "application/json"
) -> dict[str, Any]:
    if body is None:
        text = ""
    elif isinstance(body, str) and content_type != "application/json":
        text = body
    else:
        text = json.dumps(body, default=str)
    return {"statusCode": status, "headers": {"Content-Type": content_type}, "body": text}


def problem(status: int, title: str, detail: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "about:blank", "title": title, "status": status}
    if detail:
        payload["detail"] = detail
    return response(status, payload, content_type="application/problem+json")


def valid_signature(secret: bytes, message: bytes, presented: str | None) -> bool:
    """`sha256=<hex>` HMAC-SHA256, compared in constant time."""
    if not presented or not presented.startswith("sha256="):
        return False
    expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, presented.removeprefix("sha256=").strip().lower())
