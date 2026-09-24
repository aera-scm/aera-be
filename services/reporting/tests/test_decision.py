"""AT-27: record has evidence, sources and a verified audit head in both formats."""

from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from typing import Any

import pytest
from pypdf import PdfReader

from services.reporting.decision import RecordUnavailable, collect, render_html, render_pdf
from services.shared.audit import AuditWriter
from services.shared.cases import CaseStore
from services.shared.dynamo import to_item
from services.shared.models import (
    Case,
    CaseStatus,
    Figure,
    Signal,
    SignalChannel,
    SignalStatus,
    new_ulid,
)
from services.shared.signals import SignalStore

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)
CASE = "EXC-2026-0914"


def seed(client: Any, status: CaseStatus = CaseStatus.CLOSED) -> None:
    CaseStore(client, "test").create(
        Case(
            case_id=CASE,
            type="MRP_EXCEPTION",
            material="MAT-48219",
            plant="1010",
            status=status,
            tier=2,
            plan_version=1,
            rar_usd=Decimal("4720000"),
            figures=[
                Figure(
                    name="rar:SO-1/10",
                    value=Decimal("4720000"),
                    unit="USD",
                    source_ref="SAP:API_SALES_ORDER_SRV/A_SalesOrder/NetAmount",
                )
            ],
            created_at=NOW,
            updated_at=NOW,
        ),
        actor="system",
    )
    if status is not CaseStatus.CLOSED:
        return
    signal = Signal(
        signal_id=new_ulid(),
        channel=SignalChannel.EMAIL,
        sender_id="orders@krieger-guss.example",
        sender_verified=True,
        received_at=NOW,
        raw_s3_key="raw/email/source",
        raw_sha256="a" * 64,
        case_id=CASE,
        status=SignalStatus.ACCEPTED,
        normalized_text="PO 4500001234 </pre><script>alert(1)</script>",
    )
    SignalStore(client, "test").create(signal)
    records: dict[str, dict[str, Any]] = {
        "PLAN#1": {
            "plan": {"options": [{"id": "A", "costUsd": 4100, "costSourceRef": "ratecard:STO-1"}]},
            "checks": [{"checkId": "V-01", "passed": True}],
            "projection": {
                "baseline": {
                    "1010": {
                        "points": [{"at": NOW.isoformat(), "stock": 310}],
                        "stockouts": [],
                        "unitsShort": 0,
                        "sourceRefs": ["SAP:API_MATERIAL_STOCK_SRV/A_MatlStkInAcctMod"],
                    }
                },
                "projection": {
                    "1010": {
                        "points": [{"at": NOW.isoformat(), "stock": 910}],
                        "stockouts": [],
                        "unitsShort": 0,
                        "sourceRefs": ["ratecard:STO-1"],
                    }
                },
            },
        },
        "ROUTE#1": {"tier": 2},
        "PART#one": {"approver": "approver@meridian-motors.example", "decision": "APPROVED"},
        "NOTIFY#one": {"recipient": "sales@krieger-guss.example", "status": "SENT"},
    }
    for sk, content in records.items():
        client.put_item(
            TableName="aera-test-cases",
            Item=to_item(
                {
                    "PK": f"CASE#{CASE}",
                    "SK": sk,
                    **content,
                }
            ),
        )
    client.put_item(
        TableName="aera-test-dialogue",
        Item=to_item(
            {
                "PK": f"CASE#{CASE}",
                "SK": "MSG#one",
                "renderedText": "Lieferdatum?",
            }
        ),
    )
    audit = AuditWriter(client, "test")
    audit.record(
        f"CASE#{CASE}",
        "EXECUTION_COMPLETED",
        actor="system",
        case_id=CASE,
        payload={"results": [{"sapDocNumber": "4500005678"}]},
    )
    audit.record(
        f"CASE#{CASE}",
        "NOTIFICATION_SENT",
        actor="system",
        case_id=CASE,
        payload={"recipient": "sales@krieger-guss.example"},
    )


def test_at_27_export_contains_sourced_decision_and_verifiable_hash(dynamodb: Any) -> None:
    seed(dynamodb)

    record = collect(dynamodb, CASE, "test")
    html = render_html(record)
    pdf = render_pdf(record)
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)

    assert record["audit"]["verified"] is True
    assert record["audit"]["head"] == AuditWriter(dynamodb, "test").head(f"CASE#{CASE}")[0]
    assert record["projections"]["projection"]["1010"]["points"][0]["stock"] == 910
    for expected in (
        "SAP:API_SALES_ORDER_SRV",
        "V-01",
        "APPROVED",
        "4500005678",
        "NOTIFICATION_SENT",
        record["audit"]["head"],
    ):
        assert expected in html and expected in text
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert pdf.startswith(b"%PDF-")


def test_at_27_open_case_and_broken_audit_cannot_export(dynamodb: Any) -> None:
    seed(dynamodb, CaseStatus.TRIAGED)
    with pytest.raises(RecordUnavailable, match="closed or escalated"):
        collect(dynamodb, CASE, "test")
    dynamodb.update_item(
        TableName="aera-test-cases",
        Key=to_item({"PK": f"CASE#{CASE}", "SK": "META"}),
        UpdateExpression="SET #status = :closed",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=to_item({":closed": "CLOSED"}),
    )
    event = AuditWriter(dynamodb, "test").events(f"CASE#{CASE}")[0]
    dynamodb.update_item(
        TableName="aera-test-audit",
        Key=to_item({"PK": f"CASE#{CASE}", "SK": event.event_id}),
        UpdateExpression="SET #hash = :bad",
        ExpressionAttributeNames={"#hash": "hash"},
        ExpressionAttributeValues=to_item({":bad": "0" * 64}),
    )
    with pytest.raises(RecordUnavailable, match="does not verify"):
        collect(dynamodb, CASE, "test")
