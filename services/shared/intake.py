"""The first step of every channel (SRD 6.25.1 step 1, FR-ING-03, FR-ING-09).

Stores the raw payload and attachments immutably, writes the Signal as RECEIVED and emits
`SignalReceived`. Channels retry deliveries, so intake is idempotent on the channel's own
message id: a repeat either finds the finished signal (and does nothing) or resumes an
interrupted one under the same signal id.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from services.shared.dynamo import table_name
from services.shared.models import Signal, SignalChannel, SignalStatus, new_ulid
from services.shared.runtime import emit
from services.shared.signals import RawStore, SignalStore, raw_prefix

IDEMPOTENCY_DAYS = 30


@dataclass(frozen=True)
class Attachment:
    filename: str
    body: bytes
    content_type: str


@dataclass(frozen=True)
class Inbound:
    channel: SignalChannel
    sender_id: str
    body: bytes
    content_type: str
    dedup_key: str | None = None
    attachments: tuple[Attachment, ...] = ()
    normalized_text: str | None = None
    po_number: str | None = None
    material: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    actor: str = "system"


class Intake:
    def __init__(
        self,
        *,
        dynamodb: Any,
        raw: RawStore,
        bus: Any,
        component: str,
        env: str | None = None,
    ) -> None:
        self._dynamodb = dynamodb
        self._signals = SignalStore(dynamodb, env)
        self._raw = raw
        self._bus = bus
        self._component = component
        self._env = env
        self._idempotency = table_name("idempotency", env)

    def receive(self, inbound: Inbound) -> Signal:
        signal_id = self._claim(inbound) if inbound.dedup_key else new_ulid()
        existing = self._signals.get(signal_id)
        if existing is not None and existing.status is not SignalStatus.RECEIVED:
            return existing
        if existing is None:
            existing = self._store(signal_id, inbound)
        emit(
            self._bus,
            "SignalReceived",
            {
                "signalId": existing.signal_id,
                "channel": existing.channel.value,
                "rawS3Key": existing.raw_s3_key,
            },
            component=self._component,
            actor=inbound.actor,
            environment=self._env,
        )
        return existing

    def _store(self, signal_id: str, inbound: Inbound) -> Signal:
        prefix = raw_prefix(inbound.channel.value, signal_id, inbound.received_at)
        key = f"{prefix}/payload"
        digest = self._raw.put_once(key, inbound.body, inbound.content_type)
        attachments = []
        for index, attachment in enumerate(inbound.attachments, start=1):
            attachment_key = self._raw.attachment_key(prefix, index, attachment.filename)
            self._raw.put_once(attachment_key, attachment.body, attachment.content_type)
            attachments.append(attachment_key)
        signal = Signal(
            signal_id=signal_id,
            channel=inbound.channel,
            sender_id=inbound.sender_id,
            received_at=inbound.received_at,
            raw_s3_key=key,
            raw_sha256=digest,
            attachments=attachments,
            normalized_text=inbound.normalized_text,
            po_number=inbound.po_number,
            material=inbound.material,
        )
        self._signals.create(signal)
        return signal

    def _claim(self, inbound: Inbound) -> str:
        key = f"INTAKE#{inbound.channel.value}#{inbound.dedup_key}"
        candidate = new_ulid()
        try:
            self._dynamodb.put_item(
                TableName=self._idempotency,
                Item={
                    "PK": {"S": key},
                    "signalId": {"S": candidate},
                    "ttl": {"N": str(int(time.time()) + IDEMPOTENCY_DAYS * 86400)},
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
            return candidate
        except self._dynamodb.exceptions.ConditionalCheckFailedException:
            item = self._dynamodb.get_item(
                TableName=self._idempotency, Key={"PK": {"S": key}}, ConsistentRead=True
            )["Item"]
            return str(item["signalId"]["S"])
