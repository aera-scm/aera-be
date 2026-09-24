"""WP-3 definition of done, offline: every synthetic signal through the real pipeline.

Channel -> intake -> gatekeeper -> extraction -> case service, against the running Mirror,
with AWS fakes and stand-ins only for Bedrock Guardrails, Textract and Comprehend. The live
half (AT-01/AT-02 in `dev`) needs the AWS account.
"""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from generate_signals import ATTACK, Item, build

from services.case_service.handler import CaseService
from services.conftest import RAW_BUCKET, RecordingBus
from services.extraction.handler import Extraction
from services.gatekeeper.handler import Gatekeeper, Guardrail
from services.mrp_poller.handler import MrpPoller
from services.ses_inbound.handler import to_inbound
from services.shared.audit import AuditWriter
from services.shared.cases import CaseStore
from services.shared.intake import Intake
from services.shared.models import FieldStatus, SignalStatus
from services.shared.partners import load_contacts
from services.shared.sap_client import SapClient
from services.shared.signals import RawStore, SignalStore
from services.webhooks.handler import Webhooks

ENV = "test"
T0 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
APP_SECRET = "synthetic-app-secret"  # pragma: allowlist secret
CARRIER_KEY = "synthetic-carrier-key"  # pragma: allowlist secret


class Guard:
    """ApplyGuardrail stand-in: flags the prompt-attack sentence used by the data set."""

    def apply_guardrail(self, **request: Any) -> dict[str, Any]:
        text = request["content"][0]["text"]["text"]
        if ATTACK.lower()[:40] in text.lower() or "approve air freight now" in text.lower():
            return {
                "action": "GUARDRAIL_INTERVENED",
                "assessments": [
                    {
                        "contentPolicy": {
                            "filters": [{"type": "PROMPT_ATTACK", "confidence": "HIGH"}]
                        }
                    }
                ],
            }
        return {"action": "NONE"}


class Ocr:
    """Textract stand-in: the reference photo's quantity comes back at 71% confidence."""

    def analyze_document(self, **request: Any) -> dict[str, Any]:
        key = request["Document"]["S3Object"]["Name"]
        if "/whatsapp/" not in key:
            return {"Blocks": []}
        return {
            "Blocks": [
                {
                    "Id": "q",
                    "BlockType": "QUERY",
                    "Query": {"Alias": "QUANTITY"},
                    "Relationships": [{"Type": "ANSWER", "Ids": ["a"]}],
                },
                {"Id": "a", "BlockType": "QUERY_RESULT", "Text": "640", "Confidence": 71.0},
            ]
        }


class Language:
    def detect_dominant_language(self, Text: str) -> dict[str, Any]:
        return {"Languages": [{"LanguageCode": "en", "Score": 0.99}]}


def sign(key: str, message: bytes) -> str:
    return "sha256=" + hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


@pytest.fixture
def world(dynamodb: Any, s3: Any, bus: RecordingBus, sap: SapClient) -> dict[str, Any]:
    items = build(T0)
    media = {
        name.split("/")[1].removesuffix(".png"): content
        for item in items
        for name, content in item.files.items()
        if name.startswith("media/")
    }

    class Media:
        def fetch(self, media_id: str) -> tuple[bytes, str]:
            return media[media_id], "image/png"

    intake = Intake(
        dynamodb=dynamodb, raw=RawStore(s3, RAW_BUCKET), bus=bus, component="t", env=ENV
    )
    audit = AuditWriter(dynamodb, ENV)
    signals = SignalStore(dynamodb, ENV)
    guard = Guardrail(Guard(), lambda: "g", lambda: "1")
    contacts = load_contacts(sap)
    CaseStore(dynamodb, ENV).seed_counter(2026, 913)
    return {
        "items": items,
        "intake": intake,
        "hooks": Webhooks(
            intake=intake,
            whatsapp=lambda: {"appSecret": APP_SECRET},
            carrier_keys=lambda: {"1000950": CARRIER_KEY},
            media=Media(),
            clock=lambda: T0.timestamp(),
        ),
        "gatekeeper": Gatekeeper(
            signals=signals,
            raw=RawStore(s3, RAW_BUCKET),
            contacts=lambda: contacts,
            guardrail=guard,
            audit=audit,
            bus=bus,
            env=ENV,
        ),
        "extraction": Extraction(
            signals=signals,
            textract=Ocr(),
            comprehend=Language(),
            bucket=RAW_BUCKET,
            sap=sap,
            guardrail=guard,
            audit=audit,
            bus=bus,
            env=ENV,
        ),
        "cases": CaseService(dynamodb=dynamodb, sap=sap, bus=bus, clock=lambda: T0, env=ENV),
        "signals": signals,
    }


def deliver(world: dict[str, Any], item: Item, bus: RecordingBus) -> list[str]:
    before = len(bus.details("SignalReceived"))
    content = next(v for k, v in item.files.items() if not k.startswith("media/"))
    if item.channel == "EMAIL":
        world["intake"].receive(to_inbound(content))
    elif item.channel == "WHATSAPP":
        world["hooks"].handle(
            {
                "httpMethod": "POST",
                "resource": "/webhooks/whatsapp",
                "headers": {"X-Hub-Signature-256": sign(APP_SECRET, content)},
                "body": content.decode(),
            }
        )
    else:
        stamp = str(int(T0.timestamp()))
        world["hooks"].handle(
            {
                "httpMethod": "POST",
                "resource": "/webhooks/carrier",
                "headers": {
                    "X-Aera-Timestamp": stamp,
                    "X-Aera-Signature": sign(CARRIER_KEY, stamp.encode() + b"." + content),
                },
                "body": content.decode(),
            }
        )
    ids = [e["data"]["signalId"] for e in bus.details("SignalReceived")[before:]]
    for signal_id in ids:
        world["gatekeeper"].handle(signal_id)
        world["extraction"].handle(signal_id)
        world["cases"].on_signal(signal_id)
    return ids


def test_the_synthetic_set_has_the_required_mix() -> None:
    items = build(T0)
    files = [name for item in items for name in item.files]

    assert sum(1 for i in items if i.channel == "EMAIL") >= 30
    pdfs = sum(
        content.count(b"Content-Type: application/pdf")
        for item in items
        for content in item.files.values()
    )
    assert pdfs >= 10
    assert sum(1 for f in files if f.startswith("media/")) == 5
    assert sum(1 for i in items if i.channel == "CARRIER") == 3
    assert sum(1 for i in items if i.hostile) == 6


def test_wp_3_done_offline(world: dict[str, Any], sap: SapClient, bus: RecordingBus) -> None:
    started = time.monotonic()
    MrpPoller(sap=sap, bus=bus, env=ENV).poll()
    [polled] = bus.details("MrpExceptionsPolled")
    opened = world["cases"].on_mrp(polled["data"])
    assert len(opened) == 6 and opened[0] == "EXC-2026-0914"

    outcomes: dict[str, list[Any]] = {}
    for item in world["items"]:
        ids = deliver(world, item, bus)
        assert len(ids) == 1, item.id
        outcomes[item.id] = [world["signals"].get(i) for i in ids]

    for item in world["items"]:
        [signal] = outcomes[item.id]
        assert signal.status.value == item.expected, (item.id, signal.quarantine_reason)
        if item.expected == "QUARANTINED":
            assert item.reason and item.reason in (signal.quarantine_reason or ""), item.id
            assert signal.case_id is None

    # Hostile email quarantined with reason (AT-02 offline half).
    [hostile] = outcomes["hostile-01"]
    assert "kreiger-guss.example is not in SAP master data" in hostile.quarantine_reason

    # The photographed quantity is stored UNCONFIRMED; the photo joined the reference case.
    [photo] = outcomes["whatsapp-01"]
    quantity = next(f for f in photo.fields if f.name == "QUANTITY")
    assert (quantity.value, quantity.confidence, quantity.status) == (
        "640",
        0.71,
        FieldStatus.UNCONFIRMED,
    )
    assert photo.case_id == "EXC-2026-0914"

    # Email, photo and carrier events about PO 4500001234 all merged into one case (BR-15).
    reference = CaseStore(world["signals"]._client, ENV).get("EXC-2026-0914")
    joined = {
        s.signal_id
        for key in ("email-01", "whatsapp-01", "carrier-01", "carrier-02")
        for s in outcomes[key]
    }
    assert reference is not None and joined <= set(reference.signal_ids)

    # Still six actionable cases on the board, nothing opened by hostile input.
    assert bus.types().count("CaseOpened") == 6
    assert all(
        s.status is SignalStatus.QUARANTINED
        for k in outcomes
        if k.startswith("hostile")
        for s in outcomes[k]
    )
    assert json.dumps(polled["data"]["total"]) == "214"
    assert time.monotonic() - started < 120
