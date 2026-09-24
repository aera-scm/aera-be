"""Channel webhooks: WhatsApp Business Cloud API and carrier status events (IR-07, IR-08).

Every request is authenticated before anything is stored:

- `GET /webhooks/whatsapp` answers Meta's subscription check only with the configured
  verify token.
- `POST /webhooks/whatsapp` requires `X-Hub-Signature-256` (HMAC-SHA256 of the body with the
  app secret). Each message becomes one signal; an image is fetched from the Graph API and
  stored as the signal's attachment.
- `POST /webhooks/carrier` requires `X-Aera-Timestamp` and `X-Aera-Signature`, an
  HMAC-SHA256 of `<timestamp>.<body>` with that carrier's key; stale timestamps (more than
  five minutes) are refused so a captured request cannot be replayed.

Sender verification against SAP (BR-04) is the gatekeeper's job, not this handler's.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from services.shared import http
from services.shared.intake import Attachment, Inbound, Intake
from services.shared.models import SignalChannel

GRAPH = "https://graph.facebook.com/v21.0"
REPLAY_SECONDS = 300
_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}


class MediaFetcher(Protocol):
    def fetch(self, media_id: str) -> tuple[bytes, str]: ...


@dataclass
class GraphMedia:
    """Downloads WhatsApp media: resolve the id to a short-lived URL, then fetch it."""

    token: Callable[[], str]
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=10.0))

    def fetch(self, media_id: str) -> tuple[bytes, str]:
        headers = {"Authorization": f"Bearer {self.token()}"}
        meta = self.http.get(f"{GRAPH}/{media_id}", headers=headers)
        meta.raise_for_status()
        info = meta.json()
        media = self.http.get(str(info["url"]), headers=headers)
        media.raise_for_status()
        return media.content, str(info.get("mime_type") or "application/octet-stream")


@dataclass
class Webhooks:
    intake: Intake
    whatsapp: Callable[[], dict[str, str]]
    carrier_keys: Callable[[], dict[str, str]]
    media: MediaFetcher
    clock: Callable[[], float] = time.time

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        method = event.get("httpMethod")
        path = str(event.get("resource") or event.get("path") or "")
        if path.endswith("/webhooks/whatsapp") and method == "GET":
            return self._verify_subscription(event)
        if path.endswith("/webhooks/whatsapp") and method == "POST":
            return self._whatsapp(event)
        if path.endswith("/webhooks/carrier") and method == "POST":
            return self._carrier(event)
        return http.problem(404, "Not found")

    # WhatsApp ---------------------------------------------------------------------------

    def _verify_subscription(self, event: dict[str, Any]) -> dict[str, Any]:
        token = http.query(event, "hub.verify_token")
        expected = self.whatsapp().get("verifyToken")
        if http.query(event, "hub.mode") != "subscribe" or not expected or token != expected:
            return http.problem(403, "Verification failed")
        return http.response(
            200, http.query(event, "hub.challenge") or "", content_type="text/plain"
        )

    def _whatsapp(self, event: dict[str, Any]) -> dict[str, Any]:
        body = http.body_bytes(event)
        secret = self.whatsapp().get("appSecret", "").encode()
        if not secret or not http.valid_signature(
            secret, body, http.header(event, "X-Hub-Signature-256")
        ):
            return http.problem(401, "Invalid signature")
        try:
            payload = json.loads(body)
        except ValueError:
            return http.problem(400, "Body is not JSON")
        received = 0
        for value in _message_values(payload):
            for message in value.get("messages") or []:
                self.intake.receive(self._whatsapp_inbound(message, value))
                received += 1
        return http.response(200, {"received": received})

    def _whatsapp_inbound(self, message: dict[str, Any], value: dict[str, Any]) -> Inbound:
        kind = message.get("type")
        text = (message.get("text") or {}).get("body")
        attachments: tuple[Attachment, ...] = ()
        media = message.get(kind) if kind in ("image", "document") else None
        if isinstance(media, dict) and media.get("id"):
            content, mime = self.media.fetch(str(media["id"]))
            name = media.get("filename") or f"{kind}.{_EXTENSIONS.get(mime, 'bin')}"
            attachments = (Attachment(str(name), content, mime),)
            text = media.get("caption") or text
        raw = {
            "message": message,
            "metadata": value.get("metadata"),
            "contacts": value.get("contacts"),
        }
        return Inbound(
            channel=SignalChannel.WHATSAPP,
            sender_id=f"+{str(message.get('from', '')).lstrip('+')}",
            body=json.dumps(raw, sort_keys=True).encode(),
            content_type="application/json",
            dedup_key=str(message.get("id")),
            attachments=attachments,
            normalized_text=text,
            received_at=_from_epoch(message.get("timestamp")),
        )

    # Carrier ----------------------------------------------------------------------------

    def _carrier(self, event: dict[str, Any]) -> dict[str, Any]:
        body = http.body_bytes(event)
        stamp = http.header(event, "X-Aera-Timestamp") or ""
        try:
            payload = json.loads(body)
            sent = int(stamp)
        except ValueError:
            return http.problem(400, "Body must be JSON with an integer X-Aera-Timestamp")
        carrier = str(payload.get("carrierId") or "") if isinstance(payload, dict) else ""
        key = self.carrier_keys().get(carrier, "").encode()
        message = stamp.encode() + b"." + body
        if not key or not http.valid_signature(
            key, message, http.header(event, "X-Aera-Signature")
        ):
            return http.problem(401, "Invalid signature")
        if abs(self.clock() - sent) > REPLAY_SECONDS:
            return http.problem(401, "Stale timestamp")
        missing = [k for k in ("eventId", "status") if not payload.get(k)]
        if missing:
            return http.problem(400, "Missing fields", ", ".join(missing))
        signal = self.intake.receive(
            Inbound(
                channel=SignalChannel.CARRIER,
                sender_id=carrier,
                body=body,
                content_type="application/json",
                dedup_key=f"{carrier}#{payload['eventId']}",
                normalized_text=_carrier_text(payload),
                po_number=_text(payload.get("poNumber")),
                material=_text(payload.get("material")),
            )
        )
        return http.response(202, {"signalId": signal.signal_id})


def _message_values(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    return [
        change.get("value") or {}
        for entry in payload.get("entry") or []
        for change in entry.get("changes") or []
        if change.get("field") == "messages"
    ]


def _from_epoch(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _carrier_text(payload: dict[str, Any]) -> str:
    parts = [f"Carrier status {payload['status']}"]
    for label, key in (
        ("tracking", "trackingNumber"),
        ("PO", "poNumber"),
        ("material", "material"),
        ("ETA", "eta"),
        ("note", "note"),
    ):
        if payload.get(key):
            parts.append(f"{label} {payload[key]}")
    return "; ".join(parts)


_webhooks: Webhooks | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _webhooks
    if _webhooks is None:
        from services.shared import runtime
        from services.shared.signals import RawStore

        _webhooks = Webhooks(
            intake=Intake(
                dynamodb=runtime.client("dynamodb"),
                raw=RawStore(runtime.client("s3"), runtime.raw_bucket()),
                bus=runtime.client("events"),
                component="webhooks",
            ),
            whatsapp=lambda: runtime.secret("channels/whatsapp"),
            carrier_keys=lambda: runtime.secret("channels/carrier-webhook"),
            media=GraphMedia(token=lambda: runtime.secret("channels/whatsapp")["accessToken"]),
        )
    return _webhooks.handle(event)
