"""Intake, signal store and partner master data (FR-ING-03, FR-ING-09, DR-02, BR-04)."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.conftest import RAW_BUCKET, RecordingBus
from services.shared.intake import Attachment, Inbound, Intake
from services.shared.models import SignalChannel, SignalStatus
from services.shared.partners import ContactDirectory, load_contacts
from services.shared.sap_client import SapClient
from services.shared.signals import RawStore, SignalStore

ENV = "test"
AT = datetime(2026, 10, 5, 7, 30, tzinfo=UTC)


@pytest.fixture
def intake(dynamodb: Any, s3: Any, bus: RecordingBus) -> Intake:
    return Intake(
        dynamodb=dynamodb, raw=RawStore(s3, RAW_BUCKET), bus=bus, component="webhooks", env=ENV
    )


def inbound(**changes: Any) -> Inbound:
    values: dict[str, Any] = {
        "channel": SignalChannel.WHATSAPP,
        "sender_id": "+447700900234",
        "body": b'{"text": "PO 4500001234 short"}',
        "content_type": "application/json",
        "dedup_key": "wamid.1",
        "attachments": (Attachment("photo.jpg", b"\xff\xd8jpeg", "image/jpeg"),),
        "po_number": "4500001234",
        "received_at": AT,
    }
    values.update(changes)
    return Inbound(**values)


def test_fr_ing_09_raw_payload_is_stored_by_hash_and_signal_is_received(
    intake: Intake, dynamodb: Any, s3: Any, bus: RecordingBus
) -> None:
    signal = intake.receive(inbound())

    body = s3.get_object(Bucket=RAW_BUCKET, Key=signal.raw_s3_key)["Body"].read()
    assert signal.raw_sha256 == hashlib.sha256(body).hexdigest()
    assert signal.raw_s3_key.startswith(f"raw/whatsapp/2026/10/05/{signal.signal_id}/")
    assert signal.attachments == [f"raw/whatsapp/2026/10/05/{signal.signal_id}/att/01-photo.jpg"]
    stored = SignalStore(dynamodb, ENV).get(signal.signal_id)
    assert stored is not None and stored.status is SignalStatus.RECEIVED
    [event] = bus.details("SignalReceived")
    assert event["data"] == {
        "signalId": signal.signal_id,
        "channel": "WHATSAPP",
        "rawS3Key": signal.raw_s3_key,
    }


def test_fr_ing_09_raw_objects_are_never_overwritten(s3: Any) -> None:
    raw = RawStore(s3, RAW_BUCKET)
    first = raw.put_once("raw/x/payload", b"original", "text/plain")
    second = raw.put_once("raw/x/payload", b"tampered", "text/plain")

    assert first == second == hashlib.sha256(b"original").hexdigest()
    assert raw.get("raw/x/payload") == b"original"


def test_a_redelivered_message_does_not_create_a_second_signal(
    intake: Intake, dynamodb: Any, bus: RecordingBus
) -> None:
    first = intake.receive(inbound())
    store = SignalStore(dynamodb, ENV)
    store.save(first.model_copy(update={"status": SignalStatus.ACCEPTED}))

    again = intake.receive(inbound(received_at=AT + timedelta(minutes=1)))

    assert again.signal_id == first.signal_id
    assert bus.types() == ["SignalReceived"]


def test_an_interrupted_intake_resumes_under_the_same_signal_id(
    intake: Intake, bus: RecordingBus
) -> None:
    first = intake.receive(inbound())
    again = intake.receive(inbound())

    assert again.signal_id == first.signal_id
    assert bus.types() == ["SignalReceived", "SignalReceived"]  # still RECEIVED: re-announced


def test_signals_are_found_by_po_within_a_window_and_by_case(intake: Intake, dynamodb: Any) -> None:
    store = SignalStore(dynamodb, ENV)
    old = intake.receive(inbound(dedup_key="a", received_at=AT - timedelta(days=4)))
    new = intake.receive(inbound(dedup_key="b"))
    store.save(new.model_copy(update={"case_id": "EXC-2026-0914"}))

    recent = store.recent_for_po("4500001234", AT - timedelta(hours=72))

    assert [s.signal_id for s in recent] == [new.signal_id]
    assert old.signal_id not in [s.signal_id for s in store.for_case("EXC-2026-0914")]
    assert [s.signal_id for s in store.for_case("EXC-2026-0914")] == [new.signal_id]


def test_br_04_contacts_come_from_business_partner_master_data(sap: SapClient) -> None:
    contacts = {c.partner_id: c for c in load_contacts(sap)}

    assert contacts["1000234"].kind == "SUPPLIER"
    assert contacts["1000234"].email_domains == {"krieger-guss.example"}
    assert contacts["1000234"].phones == {"+447700900234"}
    assert contacts["1000950"].kind == "CARRIER"
    assert contacts["1000871"].email_domains == {"halim-presisi.example"}


def test_contact_directory_caches_for_five_minutes(sap: SapClient) -> None:
    now = [0.0]
    directory = ContactDirectory(sap, clock=lambda: now[0])
    first = directory.contacts()
    now[0] = 299.0
    assert directory.contacts() is first
    now[0] = 301.0
    assert directory.contacts() is not first
