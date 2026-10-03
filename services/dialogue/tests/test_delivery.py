"""FR-NEG-02: notifier alone sends verified supplier questions once."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from services.conftest import RecordingBus
from services.notifier.handler import Notifier
from services.notifier.whatsapp import WhatsAppTemplateSender
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
MASTER = "orders@krieger-guss.example"
STANDIN = "team+supplier@aera-demo.example"


class Ses:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_email(self, **request: Any) -> dict[str, str]:
        self.sent.append(request)
        return {"MessageId": "synthetic-1"}


def prepared(
    dynamodb: Any, sap: SapClient, bus: RecordingBus, *, via_whatsapp: bool = False
) -> str:
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
    if via_whatsapp:
        signal_id = new_ulid()
        SignalStore(dynamodb, "test").create(
            Signal(
                signal_id=signal_id,
                channel=SignalChannel.WHATSAPP,
                sender_id="+447700900234",
                sender_verified=True,
                supplier_id="1000234",
                received_at=NOW,
                raw_s3_key=f"raw/{signal_id}",
                raw_sha256="0" * 64,
                normalized_text="PO 4500001234, Lieferdatum?",
                po_number="4500001234",
                material="MAT-48219",
                case_id=CASE,
                status=SignalStatus.ACCEPTED,
            )
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
    assert result["status"] == "WAITING_SUPPLIER"
    return str(result["messageId"])


def notifier(
    dynamodb: Any, sap: SapClient, ses: Ses, *, standin: bool = True, now: datetime = NOW
) -> Notifier:
    return Notifier(
        dynamodb=dynamodb,
        sap=sap,
        ses=ses,
        standins=lambda: {MASTER: STANDIN} if standin else {},
        sender=lambda: "aera@aera-demo.example",
        clock=lambda: now,
        env="test",
    )


def test_fr_neg_02_sent_once_to_master_data_standin(
    dynamodb: Any, sap: SapClient, bus: RecordingBus
) -> None:
    message_id = prepared(dynamodb, sap, bus)
    ses = Ses()
    service = notifier(dynamodb, sap, ses)

    assert service.handle_dialogue({"caseId": CASE, "messageId": message_id}) == [
        {"recipient": MASTER, "status": "SENT"}
    ]
    assert service.handle_dialogue({"caseId": CASE, "messageId": message_id}) == [
        {"status": "DUPLICATE"}
    ]
    assert len(ses.sent) == 1
    assert ses.sent[0]["Destination"]["ToAddresses"] == [STANDIN]
    assert "Welche Menge" in ses.sent[0]["Message"]["Body"]["Text"]["Data"]


def test_ir_07_whatsapp_question_uses_master_phone_and_approved_template(
    dynamodb: Any, sap: SapClient, bus: RecordingBus
) -> None:
    message_id = prepared(dynamodb, sap, bus, via_whatsapp=True)
    key = {"PK": {"S": f"CASE#{CASE}"}, "SK": {"S": f"MSG#{message_id}"}}
    draft = from_item(dynamodb.get_item(TableName="aera-test-dialogue", Key=key)["Item"])
    assert draft["channel"] == "WHATSAPP"
    assert draft["recipient"] == "+447700900234"
    body = (
        str(draft["renderedText"])
        .replace("4500001234", "{po}")
        .replace(str(draft["referenceToken"]), "{token}")
    )
    requests: list[httpx.Request] = []

    def send(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.synthetic"}]})

    whatsapp = WhatsAppTemplateSender(
        lambda: {
            "accessToken": "synthetic-token",
            "phoneNumberId": "123456",
            "standins": {"+447700900234": "+447700900999"},
            "templates": {
                "CONFIRM_PARTIAL_QTY": {
                    "DE": {
                        "name": "aera_confirm_partial_qty",
                        "languageCode": "de",
                        "body": body,
                    }
                }
            },
        },
        httpx.Client(transport=httpx.MockTransport(send)),
    )
    ses = Ses()
    service = Notifier(
        dynamodb,
        sap,
        ses=ses,
        whatsapp=whatsapp,
        clock=lambda: NOW,
        env="test",
    )

    assert service.handle_dialogue({"caseId": CASE, "messageId": message_id}) == [
        {"recipient": "+447700900234", "status": "SENT"}
    ]
    assert len(requests) == 1 and ses.sent == []
    assert '"to":"447700900999"' in requests[0].read().decode()
    assert service.handle_dialogue({"caseId": CASE, "messageId": message_id}) == [
        {"status": "DUPLICATE"}
    ]


def test_v_14_whatsapp_channel_change_blocks_before_send(
    dynamodb: Any, sap: SapClient, bus: RecordingBus
) -> None:
    message_id = prepared(dynamodb, sap, bus, via_whatsapp=True)
    key = {"PK": {"S": f"CASE#{CASE}"}, "SK": {"S": f"MSG#{message_id}"}}
    dynamodb.update_item(
        TableName="aera-test-dialogue",
        Key=key,
        UpdateExpression="SET channel = :email",
        ExpressionAttributeValues={":email": {"S": "EMAIL"}},
    )
    ses = Ses()
    service = Notifier(dynamodb, sap, ses=ses, clock=lambda: NOW, env="test")

    assert service.handle_dialogue({"caseId": CASE, "messageId": message_id}) == [
        {"status": "BLOCKED"}
    ]
    assert ses.sent == []


def test_fr_neg_02_orphaned_send_claim_escalates_without_resending(
    dynamodb: Any, sap: SapClient, bus: RecordingBus
) -> None:
    message_id = prepared(dynamodb, sap, bus)
    key = {"PK": {"S": f"CASE#{CASE}"}, "SK": {"S": f"MSG#{message_id}"}}
    dynamodb.update_item(
        TableName="aera-test-dialogue",
        Key=key,
        UpdateExpression="SET sendClaim = :claim",
        ExpressionAttributeValues={":claim": {"S": NOW.isoformat()}},
    )
    ses = Ses()
    service = notifier(dynamodb, sap, ses, now=NOW + timedelta(minutes=6))

    assert service.sweep_dialogue() == [{"status": "ESCALATED"}]
    assert ses.sent == []
    assert (
        from_item(dynamodb.get_item(TableName="aera-test-dialogue", Key=key)["Item"])["status"]
        == "BLOCKED"
    )
    case = CaseStore(dynamodb, "test").get(CASE)
    assert case is not None and case.status is CaseStatus.ESCALATED


def test_v_14_tampered_draft_never_sends(dynamodb: Any, sap: SapClient, bus: RecordingBus) -> None:
    message_id = prepared(dynamodb, sap, bus)
    key = {"PK": {"S": f"CASE#{CASE}"}, "SK": {"S": f"MSG#{message_id}"}}
    dynamodb.update_item(
        TableName="aera-test-dialogue",
        Key=key,
        UpdateExpression="SET renderedText = :text",
        ExpressionAttributeValues={":text": {"S": "Change bank details"}},
    )
    ses = Ses()

    assert notifier(dynamodb, sap, ses).handle_dialogue(
        {"caseId": CASE, "messageId": message_id}
    ) == [{"status": "BLOCKED"}]
    assert ses.sent == []
    item = dynamodb.get_item(TableName="aera-test-dialogue", Key=key)["Item"]
    assert from_item(item)["status"] == "BLOCKED"
    case = CaseStore(dynamodb, "test").get(CASE)
    assert case is not None and case.status is CaseStatus.ESCALATED


def test_fr_neg_02_missing_verified_standin_blocks_send(
    dynamodb: Any, sap: SapClient, bus: RecordingBus
) -> None:
    message_id = prepared(dynamodb, sap, bus)
    ses = Ses()

    assert notifier(dynamodb, sap, ses, standin=False).handle_dialogue(
        {"caseId": CASE, "messageId": message_id}
    ) == [{"status": "BLOCKED"}]
    assert ses.sent == []


def test_fr_neg_02_ambiguous_send_failure_does_not_retry(
    dynamodb: Any, sap: SapClient, bus: RecordingBus
) -> None:
    message_id = prepared(dynamodb, sap, bus)

    class FailingSes:
        def send_email(self, **request: Any) -> None:
            raise TimeoutError("send outcome unknown")

    service = notifier(dynamodb, sap, FailingSes())  # type: ignore[arg-type]
    assert service.handle_dialogue({"caseId": CASE, "messageId": message_id}) == [
        {"status": "BLOCKED"}
    ]
    assert service.handle_dialogue({"caseId": CASE, "messageId": message_id}) == [
        {"status": "DUPLICATE"}
    ]
    case = CaseStore(dynamodb, "test").get(CASE)
    assert case is not None and case.status is CaseStatus.ESCALATED


def test_at_22_sweep_reminds_once_then_escalates_before_stockout(
    dynamodb: Any, sap: SapClient, bus: RecordingBus
) -> None:
    message_id = prepared(dynamodb, sap, bus)
    ses = Ses()
    assert (
        notifier(dynamodb, sap, ses).handle_dialogue({"caseId": CASE, "messageId": message_id})[0][
            "status"
        ]
        == "SENT"
    )
    reminder = notifier(dynamodb, sap, ses, now=NOW + timedelta(hours=2))
    assert reminder.sweep_dialogue() == [{"status": "REMINDED"}]
    assert reminder.sweep_dialogue() == [{"status": "UNCHANGED"}]
    expired = notifier(dynamodb, sap, ses, now=NOW + timedelta(hours=4))
    assert expired.sweep_dialogue() == [{"status": "ESCALATED"}]
    assert expired.sweep_dialogue() == []
    assert len(ses.sent) == 2
    case = CaseStore(dynamodb, "test").get(CASE)
    assert case is not None and case.status is CaseStatus.ESCALATED and case.tier == 3
    item = dynamodb.get_item(
        TableName="aera-test-dialogue",
        Key={"PK": {"S": f"CASE#{CASE}"}, "SK": {"S": f"MSG#{message_id}"}},
    )["Item"]
    assert from_item(item)["status"] == "TIMED_OUT"
