"""Channel webhooks (FR-ING-03, IR-07, IR-08): authenticate, store raw, emit SignalReceived."""

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.conftest import RAW_BUCKET, RecordingBus
from services.shared.intake import Intake
from services.shared.signals import RawStore, SignalStore
from services.webhooks.handler import GraphMedia, Webhooks

ENV = "test"
FIXTURES = Path(__file__).parent / "fixtures"
APP_SECRET = "synthetic-app-secret"  # pragma: allowlist secret
CARRIER_KEY = "synthetic-carrier-key"  # pragma: allowlist secret
NOW = 1_759_650_000


class FakeMedia:
    def __init__(self) -> None:
        self.fetched: list[str] = []

    def fetch(self, media_id: str) -> tuple[bytes, str]:
        self.fetched.append(media_id)
        return b"\xff\xd8synthetic-photo", "image/jpeg"


@pytest.fixture
def media() -> FakeMedia:
    return FakeMedia()


@pytest.fixture
def hooks(dynamodb: Any, s3: Any, bus: RecordingBus, media: FakeMedia) -> Webhooks:
    return Webhooks(
        intake=Intake(
            dynamodb=dynamodb, raw=RawStore(s3, RAW_BUCKET), bus=bus, component="webhooks", env=ENV
        ),
        whatsapp=lambda: {"appSecret": APP_SECRET, "verifyToken": "verify-me"},
        carrier_keys=lambda: {"1000950": CARRIER_KEY},
        media=media,
        clock=lambda: NOW,
    )


def sign(secret: str, message: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def post(path: str, body: bytes, headers: dict[str, str], *, b64: bool = False) -> dict[str, Any]:
    return {
        "httpMethod": "POST",
        "resource": path,
        "headers": headers,
        "body": base64.b64encode(body).decode() if b64 else body.decode(),
        "isBase64Encoded": b64,
    }


def whatsapp(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# WhatsApp -------------------------------------------------------------------------------


def test_ir_07_subscription_check_echoes_the_challenge_only_for_the_right_token(
    hooks: Webhooks,
) -> None:
    def get(token: str) -> dict[str, Any]:
        return hooks.handle(
            {
                "httpMethod": "GET",
                "resource": "/webhooks/whatsapp",
                "queryStringParameters": {
                    "hub.mode": "subscribe",
                    "hub.verify_token": token,
                    "hub.challenge": "1158201444",
                },
            }
        )

    assert get("verify-me")["body"] == "1158201444"
    assert get("verify-me")["statusCode"] == 200
    assert get("wrong")["statusCode"] == 403


def test_ir_07_photo_message_becomes_a_signal_with_the_image_attached(
    hooks: Webhooks, dynamodb: Any, s3: Any, bus: RecordingBus, media: FakeMedia
) -> None:
    body = whatsapp("whatsapp_image.json")

    result = hooks.handle(
        post("/webhooks/whatsapp", body, {"x-hub-signature-256": sign(APP_SECRET, body)}, b64=True)
    )

    assert result["statusCode"] == 200 and json.loads(result["body"]) == {"received": 1}
    assert media.fetched == ["900000000000001"]
    [event] = bus.details("SignalReceived")
    signal = SignalStore(dynamodb, ENV).get(event["data"]["signalId"])
    assert signal is not None
    assert signal.sender_id == "+447700900234"
    assert signal.normalized_text is not None and "4500001234" in signal.normalized_text
    [photo] = signal.attachments
    assert photo.endswith("/att/01-image.jpg")
    assert s3.get_object(Bucket=RAW_BUCKET, Key=photo)["Body"].read() == b"\xff\xd8synthetic-photo"


@pytest.mark.parametrize("signature", [None, "sha256=00", "md5=abc"])
def test_ir_07_unsigned_or_wrongly_signed_posts_store_nothing(
    hooks: Webhooks, bus: RecordingBus, signature: str | None
) -> None:
    headers = {"X-Hub-Signature-256": signature} if signature else {}

    result = hooks.handle(post("/webhooks/whatsapp", whatsapp("whatsapp_image.json"), headers))

    assert result["statusCode"] == 401
    assert result["headers"]["Content-Type"] == "application/problem+json"
    assert bus.entries == []


def test_ir_07_delivery_receipts_and_redeliveries_create_no_new_signal(
    hooks: Webhooks, bus: RecordingBus
) -> None:
    status = whatsapp("whatsapp_status.json")
    image = whatsapp("whatsapp_image.json")

    hooks.handle(
        post("/webhooks/whatsapp", status, {"X-Hub-Signature-256": sign(APP_SECRET, status)})
    )
    for _ in range(2):
        hooks.handle(
            post("/webhooks/whatsapp", image, {"X-Hub-Signature-256": sign(APP_SECRET, image)})
        )

    signal_ids = {e["data"]["signalId"] for e in bus.details("SignalReceived")}
    assert len(signal_ids) == 1


def test_graph_media_resolves_the_id_then_downloads_with_the_token() -> None:
    seen: list[tuple[str, str | None]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("Authorization")))
        if request.url.host == "graph.facebook.com":
            return httpx.Response(
                200, json={"url": "https://lookaside.example/m/1", "mime_type": "image/jpeg"}
            )
        return httpx.Response(200, content=b"jpeg")

    fetcher = GraphMedia(
        token=lambda: "synthetic-token", http=httpx.Client(transport=httpx.MockTransport(respond))
    )

    assert fetcher.fetch("900") == (b"jpeg", "image/jpeg")
    assert seen == [
        ("https://graph.facebook.com/v21.0/900", "Bearer synthetic-token"),
        ("https://lookaside.example/m/1", "Bearer synthetic-token"),
    ]


# Carrier --------------------------------------------------------------------------------

CARRIER_EVENT = {
    "carrierId": "1000950",
    "eventId": "NFL-EVT-7781",
    "trackingNumber": "NFL-SEA-448120",
    "poNumber": "4500001234",
    "material": "MAT-48219",
    "status": "DELAYED",
    "eta": "2026-10-14T06:00:00Z",
    "note": "Vessel held at Port Klang",
}


def carrier_request(
    payload: dict[str, Any], *, at: int = NOW, key: str = CARRIER_KEY
) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    stamp = str(at)
    return post(
        "/webhooks/carrier",
        body,
        {"X-Aera-Timestamp": stamp, "X-Aera-Signature": sign(key, stamp.encode() + b"." + body)},
    )


def test_ir_08_signed_carrier_event_becomes_a_signal(
    hooks: Webhooks, dynamodb: Any, bus: RecordingBus
) -> None:
    result = hooks.handle(carrier_request(CARRIER_EVENT))

    assert result["statusCode"] == 202
    signal = SignalStore(dynamodb, ENV).get(json.loads(result["body"])["signalId"])
    assert signal is not None
    assert (signal.channel.value, signal.sender_id) == ("CARRIER", "1000950")
    assert (signal.po_number, signal.material) == ("4500001234", "MAT-48219")
    assert signal.normalized_text is not None and "DELAYED" in signal.normalized_text
    assert bus.types() == ["SignalReceived"]


@pytest.mark.parametrize(
    "request_",
    [
        carrier_request(CARRIER_EVENT, key="wrong-key"),
        carrier_request({**CARRIER_EVENT, "carrierId": "1000234"}),
        carrier_request(CARRIER_EVENT, at=NOW - 301),
    ],
    ids=["bad-signature", "unknown-carrier", "replayed"],
)
def test_ir_08_unauthenticated_carrier_events_are_refused(
    hooks: Webhooks, bus: RecordingBus, request_: dict[str, Any]
) -> None:
    assert hooks.handle(request_)["statusCode"] == 401
    assert bus.entries == []


def test_ir_08_malformed_carrier_events_are_rejected(hooks: Webhooks) -> None:
    assert hooks.handle(carrier_request({"carrierId": "1000950"}))["statusCode"] == 400
    bad = post("/webhooks/carrier", b"not json", {"X-Aera-Timestamp": str(NOW)})
    assert hooks.handle(bad)["statusCode"] == 400


def test_unknown_routes_are_404(hooks: Webhooks) -> None:
    assert hooks.handle({"httpMethod": "PUT", "resource": "/webhooks/x"})["statusCode"] == 404
