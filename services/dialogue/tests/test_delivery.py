"""FR-NEG-02: notifier alone sends verified supplier questions once."""

from datetime import UTC, datetime
from typing import Any

from services.conftest import RecordingBus
from services.notifier.handler import Notifier
from services.shared.cases import CaseStore
from services.shared.dynamo import from_item
from services.shared.models import Case, CaseStatus
from services.shared.sap_client import SapClient
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


def prepared(dynamodb: Any, sap: SapClient, bus: RecordingBus) -> str:
    CaseStore(dynamodb, "test").create(
        Case(
            case_id=CASE, type="SUPPLIER_DELAY", material="MAT-48219", plant="1010",
            po_number="4500001234", po_item="10", status=CaseStatus.INVESTIGATING,
            created_at=NOW, updated_at=NOW,
        ), actor="system",
    )
    ctx = ToolContext(sap=sap, dynamodb=dynamodb, bus=bus, clock=lambda: NOW, env="test")
    result = BY_NAME["request_supplier_info"].invoke(ctx, {
        "caseId": CASE, "templateId": "CONFIRM_PARTIAL_QTY",
        "fields": {"poNumber": "4500001234"},
    })
    assert result["status"] == "WAITING_SUPPLIER"
    return str(result["messageId"])


def notifier(dynamodb: Any, sap: SapClient, ses: Ses, *, standin: bool = True) -> Notifier:
    return Notifier(
        dynamodb=dynamodb, sap=sap, ses=ses,
        standins=lambda: {MASTER: STANDIN} if standin else {},
        sender=lambda: "aera@aera-demo.example", clock=lambda: NOW, env="test",
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


def test_v_14_tampered_draft_never_sends(
    dynamodb: Any, sap: SapClient, bus: RecordingBus
) -> None:
    message_id = prepared(dynamodb, sap, bus)
    key = {"PK": {"S": f"CASE#{CASE}"}, "SK": {"S": f"MSG#{message_id}"}}
    dynamodb.update_item(
        TableName="aera-test-dialogue", Key=key,
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
