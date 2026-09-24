"""FR-RPT-01 / AT-27: export one source-backed, hash-verified case decision."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from reportlab.lib import colors  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # type: ignore[import-untyped]
from reportlab.lib.units import mm  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from services.shared.audit import GENESIS, AuditWriter, verify_chain
from services.shared.cases import CaseStore
from services.shared.dynamo import from_item, table_name
from services.shared.models import CaseStatus
from services.shared.signals import SignalStore


class RecordUnavailable(ValueError):
    pass


def _rows(client: Any, table: str, pk: str) -> list[dict[str, Any]]:
    request: dict[str, Any] = {
        "TableName": table,
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": {"S": pk}},
        "ConsistentRead": True,
    }
    rows: list[dict[str, Any]] = []
    while True:
        page = client.query(**request)
        rows.extend(from_item(item, keep_decimals=False) for item in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return rows
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def collect(client: Any, case_id: str, env: str) -> dict[str, Any]:
    case = CaseStore(client, env).get(case_id)
    if case is None:
        raise RecordUnavailable("case not found")
    if case.status not in {CaseStatus.CLOSED, CaseStatus.ESCALATED}:
        raise RecordUnavailable("decision record requires a closed or escalated case")
    audit = AuditWriter(client, env)
    chain = f"CASE#{case_id}"
    events = audit.events(chain)
    head, last_id = audit.head(chain)
    if (
        not events
        or verify_chain(events)
        or head != events[-1].hash
        or last_id != events[-1].event_id
    ):
        raise RecordUnavailable("case audit chain does not verify")
    items = _rows(client, table_name("cases", env), chain)
    plan = next((i for i in items if i.get("SK") == f"PLAN#{case.plan_version}"), {})
    route = next((i for i in items if i.get("SK") == f"ROUTE#{case.plan_version}"), {})
    approvals = [i for i in items if str(i.get("SK", "")).startswith("PART#")]
    notifications = [i for i in items if str(i.get("SK", "")).startswith("NOTIFY#")]
    dialogue = _rows(client, table_name("dialogue", env), chain)
    signals = SignalStore(client, env).for_case(case_id)
    return {
        "caseId": case_id,
        "synthetic": True,
        "basis": "SAP Mirror reference or Scenario Lab data",
        "createdAt": datetime.now(UTC).isoformat(),
        "case": case.model_dump(mode="json", by_alias=True),
        "evidence": [s.model_dump(mode="json", by_alias=True) for s in signals],
        "figures": [f.model_dump(mode="json", by_alias=True) for f in case.figures],
        "projections": plan.get("projection"),
        "options": (plan.get("plan") or {}).get("options", []),
        "checks": plan.get("checks", []),
        "tier": route.get("tier", case.tier),
        "approvals": approvals,
        "writes": [
            e.model_dump(mode="json", by_alias=True)
            for e in events
            if e.type.startswith(("EXECUTION_", "ACTION_", "UNDO_", "ROLLBACK_"))
            or e.type in {"FAILED_ROLLED_BACK", "COMPENSATION_FAILED"}
        ],
        "messages": {
            "notifications": notifications,
            "dialogue": dialogue,
            "auditEvents": [
                e.model_dump(mode="json", by_alias=True)
                for e in events
                if e.type.startswith(("NOTIFICATION_", "SUPPLIER_"))
            ],
        },
        "audit": {
            "head": head,
            "genesis": GENESIS,
            "eventCount": len(events),
            "lastEventId": last_id,
            "verified": True,
        },
    }


def _summary(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, default=str, indent=2)


def _projection_summary(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["No projection snapshot was recorded for this plan."]
    lines = []
    for kind in ("baseline", "projection"):
        for plant, detail in sorted((value.get(kind) or {}).items()):
            points = detail.get("points") or []
            lines.append(
                f"{kind.title()} - plant {plant}: {len(points)} points; "
                f"stockouts {_summary(detail.get('stockouts', []))}; "
                f"units short {detail.get('unitsShort')}; "
                f"sources {', '.join(detail.get('sourceRefs', []))}; "
                f"first point {_summary(points[0]) if points else 'none'}; "
                f"last point {_summary(points[-1]) if points else 'none'}"
            )
    return lines or ["Projection snapshot contains no plants."]


def sections(record: dict[str, Any]) -> list[tuple[str, list[str]]]:
    case = record["case"]
    return [
        (
            "Case",
            [
                f"{case['caseId']} | {case['status']} | {case['material']} | plant {case['plant']}",
                f"Created {case['createdAt']} | Updated {case['updatedAt']}",
            ],
        ),
        ("Evidence", [_summary(row) for row in record["evidence"]] or ["None recorded."]),
        ("Figures and sources", [_summary(row) for row in record["figures"]] or ["None recorded."]),
        ("Time-phased projections", _projection_summary(record["projections"])),
        ("Options", [_summary(row) for row in record["options"]] or ["None recorded."]),
        ("Verifier checks", [_summary(row) for row in record["checks"]] or ["None recorded."]),
        (
            "Tier and approvals",
            [f"Tier {record['tier']}", *[_summary(row) for row in record["approvals"]]],
        ),
        (
            "Writes and undo",
            [_summary(row) for row in record["writes"]] or ["No execution writes recorded."],
        ),
        ("Messages", [_summary(record["messages"])]),
        (
            "Audit chain",
            [
                f"Verified: {record['audit']['verified']}",
                f"Events: {record['audit']['eventCount']}",
                f"Head: {record['audit']['head']}",
                f"Last event: {record['audit']['lastEventId']}",
            ],
        ),
    ]


def render_html(record: dict[str, Any]) -> str:
    parts = [
        "<!doctype html><html lang='en'><meta charset='utf-8'>",
        "<title>AERA decision record</title>",
        "<style>body{font:15px/1.55 Georgia,serif;max-width:900px;margin:40px auto;"
        "padding:0 24px;color:#183747;background:#f6f7f3}h1,h2{font-family:Arial,sans-serif}"
        "h1{font-size:32px}h2{margin-top:30px;border-bottom:1px solid #a9c2c2;padding-bottom:6px}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:12px;"
        "border-left:3px solid #277b8b}small{color:#526972}</style>",
        f"<h1>Decision record - {html.escape(record['caseId'])}</h1>",
        "<p><strong>Synthetic SAP Mirror data.</strong> "
        "Values and actions below come from this case record.</p>",
    ]
    for title, entries in sections(record):
        parts.append(f"<section><h2>{html.escape(title)}</h2>")
        parts.extend(f"<pre>{html.escape(entry)}</pre>" for entry in entries)
        parts.append("</section>")
    parts.append("</html>")
    return "".join(parts)


def render_pdf(record: dict[str, Any]) -> bytes:
    target = BytesIO()
    document = SimpleDocTemplate(
        target,
        pagesize=A4,
        leftMargin=19 * mm,
        rightMargin=19 * mm,
        topMargin=20 * mm,
        bottomMargin=19 * mm,
        title=f"AERA decision record {record['caseId']}",
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "RecordTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=19,
        leading=23,
        textColor=colors.HexColor("#173d50"),
    )
    heading = ParagraphStyle(
        "RecordHeading",
        parent=styles["Heading2"],
        spaceBefore=12,
        textColor=colors.HexColor("#216b7b"),
    )
    body = ParagraphStyle(
        "RecordBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        spaceAfter=5,
        splitLongWords=1,
    )
    story: list[Any] = [
        Paragraph(f"Decision record - {html.escape(record['caseId'])}", title),
        Paragraph("Synthetic SAP Mirror data | Verified audit chain", body),
        HRFlowable(width="100%", thickness=1, color=colors.HexColor("#6d9da6")),
        Spacer(1, 5 * mm),
    ]
    for section_title, entries in sections(record):
        story.append(Paragraph(html.escape(section_title), heading))
        for entry in entries:
            story.append(Paragraph(html.escape(entry).replace("\n", "<br/>"), body))
    document.build(story)
    return target.getvalue()
