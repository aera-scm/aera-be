"""Supplier email: SES receipt rule writes the message to S3, this turns it into a signal
(FR-ING-03, IR-06 inbound).

The message is parsed with the standard library; the visible body becomes the signal text,
PDF and image parts become attachments. The complete original message is the raw payload,
so the gatekeeper can still see hidden HTML and SES's SPF/DKIM verdicts.
"""

from __future__ import annotations

import email
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import default
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any
from urllib.parse import unquote_plus

from services.shared.intake import Attachment, Inbound, Intake
from services.shared.models import Signal, SignalChannel
from services.shared.text import visible_html_text

ATTACHMENT_TYPES = ("application/pdf", "image/jpeg", "image/png", "image/tiff")


def parse(raw: bytes) -> EmailMessage:
    message = email.message_from_bytes(raw, policy=default)
    assert isinstance(message, EmailMessage)
    return message


def body_text(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = str(part.get_content())
    if part.get_content_type() == "text/html":
        return visible_html_text(content)
    return content.strip()


def attachments(message: EmailMessage) -> tuple[Attachment, ...]:
    found = []
    for part in message.iter_attachments():
        content_type = part.get_content_type()
        if content_type not in ATTACHMENT_TYPES:
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            found.append(Attachment(part.get_filename() or "attachment", payload, content_type))
    return tuple(found)


def to_inbound(raw: bytes) -> Inbound:
    message = parse(raw)
    sender = parseaddr(str(message.get("From", "")))[1] or str(message.get("From", ""))
    subject = str(message.get("Subject", "")).strip()
    text = "\n\n".join(part for part in (subject, body_text(message)) if part)
    try:
        received = parsedate_to_datetime(str(message["Date"])).astimezone(UTC)
    except (TypeError, ValueError):
        received = datetime.now(UTC)
    return Inbound(
        channel=SignalChannel.EMAIL,
        sender_id=sender,
        body=raw,
        content_type="message/rfc822",
        dedup_key=str(message.get("Message-ID") or "").strip() or None,
        attachments=attachments(message),
        normalized_text=text,
        received_at=received,
    )


@dataclass
class SesInbound:
    intake: Intake
    s3: Any

    def handle(self, event: dict[str, Any]) -> list[Signal]:
        if event.get("detail-type") == "Object Created":
            # The raw bucket's EventBridge notification (the deployed trigger); its keys are
            # plain, unlike S3 notification records.
            detail = event["detail"]
            found = [(detail["bucket"]["name"], detail["object"]["key"], event.get("time"))]
        else:
            found = [
                (
                    record["s3"]["bucket"]["name"],
                    unquote_plus(record["s3"]["object"]["key"]),
                    record.get("eventTime"),
                )
                for record in event.get("Records") or []
            ]
        signals = []
        for bucket, key, at in found:
            raw = self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            inbound = to_inbound(raw)
            if at:
                # The Date: header is the sender's claim; SES's receipt time is ours.
                received = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
                inbound = replace(inbound, received_at=received.astimezone(UTC))
            signals.append(self.intake.receive(inbound))
        return signals


_service: SesInbound | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _service
    if _service is None:
        from services.shared import runtime
        from services.shared.signals import RawStore

        s3 = runtime.client("s3")
        _service = SesInbound(
            intake=Intake(
                dynamodb=runtime.client("dynamodb"),
                raw=RawStore(s3, runtime.raw_bucket()),
                bus=runtime.client("events"),
                component="ses-inbound",
            ),
            s3=s3,
        )
    return {"signalIds": [s.signal_id for s in _service.handle(event)]}
