"""Replay of the synthetic set into a deployed environment (WP-3, M1 exit checklist)."""

import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import boto3
import httpx
import pytest
from generate_signals import build
from moto import mock_aws
from replay_signals import Target, replay

T0 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
TARGET = Target(
    api_url="https://api.example/dev/",
    raw_bucket="aera-dev-raw-000000000000-us-east-1",
    app_secret="synthetic-app",  # pragma: allowlist secret
    carrier_key="synthetic-carrier",  # pragma: allowlist secret
)


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # pragma: allowlist secret
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=TARGET.raw_bucket)
        yield client


def test_every_item_goes_where_the_real_channel_would_put_it(s3: Any) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202 if request.url.path.endswith("carrier") else 200)

    items = build(T0)
    results = replay(
        items,
        TARGET,
        s3=s3,
        http=httpx.Client(transport=httpx.MockTransport(respond)),
        clock=lambda: T0.timestamp(),
        run_id="r1",
    )

    assert len(results) == len(items)
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=TARGET.raw_bucket)["Contents"]}
    emails = [i for i in items if i.channel == "EMAIL"]
    assert {f"ses/replay-r1-{i.id}" for i in emails} <= keys
    assert sum(1 for k in keys if k.startswith("replay-media/")) == 5

    for request in requests:
        body = request.content
        if request.url.path.endswith("/webhooks/whatsapp"):
            expected = hmac.new(b"synthetic-app", body, hashlib.sha256).hexdigest()
            assert request.headers["X-Hub-Signature-256"] == f"sha256={expected}"
        else:
            stamp = request.headers["X-Aera-Timestamp"]
            message = stamp.encode() + b"." + body
            expected = hmac.new(b"synthetic-carrier", message, hashlib.sha256).hexdigest()
            assert request.headers["X-Aera-Signature"] == f"sha256={expected}"
            assert json.loads(body)["carrierId"] == "1000950"
    assert sum(1 for r in requests if r.url.path == "/dev/webhooks/whatsapp") == 5
    assert sum(1 for r in requests if r.url.path == "/dev/webhooks/carrier") == 3
