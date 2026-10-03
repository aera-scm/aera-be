"""FR-LAB-02: channel replay enters deployed SES and signed webhook paths."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from services.conftest import RAW_BUCKET, RecordingBus
from services.lab.delivery import ChannelReplay
from services.lab.scenario import Parameters, generate
from services.lab.service import Lab
from services.ses_inbound.handler import SesInbound
from services.shared.intake import Intake
from services.shared.signals import RawStore
from services.webhooks.handler import ReplayMedia, Webhooks

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)
RUN_ID = "01KTEST123456789ABCDEFGHJK"
APP_SECRET = "synthetic-app-secret"  # pragma: allowlist secret
CARRIER_KEY = "synthetic-carrier-key"  # pragma: allowlist secret


def params(channel: str) -> Parameters:
    return Parameters.model_validate(
        {
            "exceptionType": "CARRIER_DELAY" if channel == "CARRIER" else "SUPPLIER_DELAY",
            "material": "MAT-48219",
            "plant": "1010",
            "daysLate": 3,
            "quantityShort": 400,
            "channel": channel,
            "language": "EN",
            "hostile": True,
        }
    )


@pytest.mark.parametrize("channel", ["EMAIL", "WHATSAPP", "CARRIER"])
def test_fr_lab_02_channel_replay_uses_receipt_or_signed_webhook(
    dynamodb: Any, s3: Any, bus: RecordingBus, channel: str
) -> None:
    raw = RawStore(s3, RAW_BUCKET)
    intake = Intake(dynamodb=dynamodb, raw=raw, bus=bus, component="test", env="test")
    ses = SesInbound(intake, s3)
    hooks = Webhooks(
        intake,
        whatsapp=lambda: {"appSecret": APP_SECRET},
        carrier_keys=lambda: {"1000950": CARRIER_KEY},
        media=ReplayMedia(raw),
        clock=lambda: NOW.timestamp(),
    )
    posted: list[tuple[str, bytes]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = request.content
        posted.append((request.url.path, body))
        if request.url.path.endswith("/whatsapp"):
            expected = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
            assert request.headers["X-Hub-Signature-256"] == expected
        event = {
            "httpMethod": "POST",
            "resource": request.url.path.removeprefix("/dev"),
            "headers": dict(request.headers),
            "body": body.decode(),
        }
        result = hooks.handle(event)
        return httpx.Response(result["statusCode"], json=json.loads(result["body"]))

    delivery = ChannelReplay(
        s3=s3,
        raw_bucket=RAW_BUCKET,
        api_url=lambda: "https://api.example/dev",
        whatsapp_secret=lambda: APP_SECRET,
        carrier_key=lambda: CARRIER_KEY,
        http=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    selected = params(channel)
    receipts = delivery.deliver(selected, generate(selected, NOW, RUN_ID), RUN_ID, NOW)
    assert receipts[0][0] == channel
    if channel == "EMAIL":
        assert posted == []
    else:
        assert posted and posted[0][0].endswith(
            channel.lower() if channel != "WHATSAPP" else "whatsapp"
        )
    for suffix in ["main", "hostile"] if channel == "EMAIL" else ["hostile"]:
        key = f"ses/lab-{RUN_ID}-{suffix}"
        [signal] = ses.handle(
            {
                "detail-type": "Object Created",
                "time": NOW.isoformat(),
                "detail": {"bucket": {"name": RAW_BUCKET}, "object": {"key": key}},
            }
        )
        assert signal.channel.value == "EMAIL"
    lab = Lab(dynamodb, intake, lambda _: None, delivery=delivery, clock=lambda: NOW, env="test")
    row = lab.start(selected.model_dump(mode="json", by_alias=True), "user:admin")
    if channel == "EMAIL":
        for suffix in ("main", "hostile"):
            ses.handle(
                {
                    "detail-type": "Object Created",
                    "time": NOW.isoformat(),
                    "detail": {
                        "bucket": {"name": RAW_BUCKET},
                        "object": {"key": f"ses/lab-{row['runId']}-{suffix}"},
                    },
                }
            )
    else:
        ses.handle(
            {
                "detail-type": "Object Created",
                "time": NOW.isoformat(),
                "detail": {
                    "bucket": {"name": RAW_BUCKET},
                    "object": {"key": f"ses/lab-{row['runId']}-hostile"},
                },
            }
        )
    result = lab.get(row["runId"])
    assert result["deliveryMode"] == "channel-replay"
    assert len(result["signalIds"]) == 2
    assert bus.types().count("SignalReceived") >= 2
