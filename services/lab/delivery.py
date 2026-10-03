"""Synthetic Lab replay through deployed channel entry points (FR-LAB-02).

Email takes the SES receipt-rule S3 path; WhatsApp and carrier use signed public
webhooks. The WhatsApp image uses the explicitly configured replay-media adapter.
This mode is channel replay, not Meta or SES provider traffic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from services.lab.scenario import CONTACTS, SEED, Artifacts, Parameters
from services.shared.signals import RawStore


def _signature(key: str, body: bytes) -> str:
    return "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


@dataclass
class ChannelReplay:
    s3: Any
    raw_bucket: str
    api_url: Callable[[], str]
    whatsapp_secret: Callable[[], str]
    carrier_key: Callable[[], str]
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=15.0))
    raw_store: RawStore | None = None

    def deliver(
        self, params: Parameters, artifacts: Artifacts, run_id: str, now: datetime
    ) -> list[tuple[str, str]]:
        _, _, supplier = SEED[params.material]
        receipts: list[tuple[str, str]] = []
        if params.channel == "EMAIL":
            self._email(artifacts.email, run_id, "main")
            receipts.append(("EMAIL", f"<lab-{run_id}-main@aera-demo.example>"))
        elif params.channel == "WHATSAPP":
            media_id = str(int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:7], "big"))
            self.s3.put_object(
                Bucket=self.raw_bucket,
                Key=f"replay-media/{media_id}.png",
                Body=artifacts.photo,
                ContentType="image/png",
            )
            message_id = f"lab-{run_id}-photo"
            payload = {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "messages": [
                                        {
                                            "from": CONTACTS[supplier][1].lstrip("+"),
                                            "id": message_id,
                                            "timestamp": str(int(now.timestamp())),
                                            "type": "image",
                                            "image": {"id": media_id, "caption": artifacts.text},
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ],
            }
            body = json.dumps(payload, separators=(",", ":")).encode()
            self._post(
                "/webhooks/whatsapp",
                body,
                {"X-Hub-Signature-256": _signature(self.whatsapp_secret(), body)},
                200,
            )
            receipts.append(("WHATSAPP", message_id))
        else:
            stamp = str(int(now.timestamp()))
            body = artifacts.carrier_event
            self._post(
                "/webhooks/carrier",
                body,
                {
                    "X-Aera-Timestamp": stamp,
                    "X-Aera-Signature": _signature(
                        self.carrier_key(), stamp.encode() + b"." + body
                    ),
                },
                202,
            )
            receipts.append(("CARRIER", f"1000950#lab-{run_id}"))
        if artifacts.hostile_email is not None:
            self._email(artifacts.hostile_email, run_id, "hostile")
            receipts.append(("EMAIL", f"<lab-{run_id}-hostile@aera-demo.example>"))
        return receipts

    def _email(self, message: bytes, run_id: str, suffix: str) -> None:
        store = self.raw_store or RawStore(self.s3, self.raw_bucket)
        key = f"ses/lab-{run_id}-{suffix}"
        store.put_once(key, message, "message/rfc822")

    def _post(self, path: str, body: bytes, headers: dict[str, str], expected: int) -> None:
        response = self.http.post(
            f"{self.api_url().rstrip('/')}{path}",
            content=body,
            headers={"Content-Type": "application/json", **headers},
        )
        if response.status_code != expected:
            raise ValueError(f"Lab channel replay refused with HTTP {response.status_code}")
