"""AT-20/21: only a gated, token-matched supplier reply resumes investigation."""

from datetime import UTC, datetime, timedelta
from typing import Any

from services.conftest import RecordingBus
from services.dialogue.replies import ReplyMatcher
from services.notifier.handler import Notifier
from services.outbox.handler import OutboxRelay
from services.shared.cases import CaseStore
from services.shared.dynamo import from_item
from services.shared.models import (
    Case,
    CaseStatus,
    Signal,
    SignalChannel,
    SignalStatus,
    new_ulid,
)
from services.shared.sap_client import SapClient
from services.shared.signals import SignalStore
from services.tools.context import ToolContext
from services.tools.registry import BY_NAME

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)
CASE = "EXC-2026-0914"


class Ses:
    def send_email(self, **request: Any) -> dict[str, str]:
        return {"MessageId": "synthetic"}


def prepared(dynamodb: Any, sap: SapClient, bus: RecordingBus) -> tuple[str, str]:
    CaseStore(dynamodb, "test").create(
        Case(
            case_id=CASE,
            type="SUPPLIER_DELAY",
            material="MAT-48219",
            plant="1010",
            po_number="4500001234",
            po_item="10",
            status=CaseStatus.INVESTIGATING,
            stockout_at=NOW + timedelta(hours=6),
            created_at=NOW,
            updated_at=NOW,
        ),
        actor="system",
    )
    ctx = ToolContext(sap=sap, dynamodb=dynamodb, bus=bus, clock=lambda: NOW, env="test")
    result = BY_NAME["request_supplier_info"].invoke(
        ctx,
        {
            "caseId": CASE,
            "templateId": "CONFIRM_PARTIAL_QTY",
            "fields": {"poNumber": "4500001234"},
        },
    )
    message_id = str(result["messageId"])
    service = Notifier(
        dynamodb=dynamodb,
        sap=sap,
        ses=Ses(),
        standins=lambda: {"orders@krieger-guss.example": "team@example.test"},
        sender=lambda: "aera@example.test",
        clock=lambda: NOW,
        env="test",
    )
    assert service.handle_dialogue({"caseId": CASE, "messageId": message_id})[0]["status"] == "SENT"
    item = dynamodb.get_item(
        TableName="aera-test-dialogue",
        Key={"PK": {"S": f"CASE#{CASE}"}, "SK": {"S": f"MSG#{message_id}"}},
    )["Item"]
    return message_id, str(from_item(item)["referenceToken"])


def reply(
    dynamodb: Any,
    token: str,
    *,
    supplier: str = "1000234",
    status: SignalStatus = SignalStatus.ACCEPTED,
    received_at: datetime = NOW + timedelta(hours=1),
) -> str:
    signal_id = new_ulid()
    SignalStore(dynamodb, "test").create(
        Signal(
            signal_id=signal_id,
            channel=SignalChannel.EMAIL,
            sender_id="orders@krieger-guss.example",
            sender_verified=True,
            supplier_id=supplier,
            received_at=received_at,
            raw_s3_key=f"raw/{signal_id}",
            raw_sha256="0" * 64,
            normalized_text=f"Reference: {token}. Available quantity: 640.",
            po_number="4500001234",
            material="MAT-48219",
            case_id=CASE,
            status=status,
        )
    )
    return signal_id


def test_at_20_german_question_reply_resumes_case(
    dynamodb: Any, sap: SapClient, bus: RecordingBus
) -> None:
    message_id, token = prepared(dynamodb, sap, bus)
    signal_id = reply(dynamodb, token)

    assert ReplyMatcher(dynamodb, bus, "test").match(signal_id)
    case = CaseStore(dynamodb, "test").get(CASE)
    assert case is not None and case.status is CaseStatus.WAITING_SUPPLIER
    outbox = dynamodb.query(
        TableName="aera-test-cases",
        KeyConditionExpression="PK = :case AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={
            ":case": {"S": f"CASE#{CASE}"},
            ":prefix": {"S": "OUTBOX#SUPPLIER_REPLY#"},
        },
    )["Items"]
    assert len(outbox) == 2
    assert not bus.details("CaseReadyForRun")
    OutboxRelay(dynamodb, bus, "test").relay(
        {"Records": [{"eventName": "INSERT", "dynamodb": {"NewImage": item}} for item in outbox]}
    )
    assert bus.details("CaseReadyForRun")[-1]["data"]["signalId"] == signal_id
    assert not ReplyMatcher(dynamodb, bus, "test").match(signal_id)
    item = dynamodb.get_item(
        TableName="aera-test-dialogue",
        Key={"PK": {"S": f"CASE#{CASE}"}, "SK": {"S": f"MSG#{message_id}"}},
    )["Item"]
    assert from_item(item)["status"] == "REPLIED"


def test_at_21_wrong_supplier_or_quarantined_reply_cannot_resume(
    dynamodb: Any, sap: SapClient, bus: RecordingBus
) -> None:
    _, token = prepared(dynamodb, sap, bus)
    matcher = ReplyMatcher(dynamodb, bus, "test")
    assert not matcher.match(reply(dynamodb, token, supplier="attacker"))
    assert not matcher.match(reply(dynamodb, token, status=SignalStatus.QUARANTINED))
    case = CaseStore(dynamodb, "test").get(CASE)
    assert case is not None and case.status is CaseStatus.WAITING_SUPPLIER


def test_fr_neg_03_expired_reply_does_not_resume(
    dynamodb: Any, sap: SapClient, bus: RecordingBus
) -> None:
    _, token = prepared(dynamodb, sap, bus)
    assert not ReplyMatcher(dynamodb, bus, "test").match(
        reply(dynamodb, token, received_at=NOW + timedelta(hours=4))
    )
