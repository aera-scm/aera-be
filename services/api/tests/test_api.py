"""Console API, M1 routes (SRD 6.10, FR-TRI-01/02, FR-UI-05, NFR-SEC-04, BR-02)."""

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from services.api.handler import Api
from services.conftest import RAW_BUCKET, RecordingBus
from services.shared.audit import AuditWriter
from services.shared.cases import CaseStore
from services.shared.dynamo import to_item
from services.shared.intake import Inbound, Intake
from services.shared.models import (
    Case,
    CaseStatus,
    ExtractedField,
    FieldStatus,
    SignalChannel,
    SignalStatus,
    TraceEvent,
)
from services.shared.signals import RawStore, SignalStore
from services.shared.trace import TraceStore

ENV = "test"
T0 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)


@pytest.fixture
def intake(dynamodb: Any, s3: Any, bus: RecordingBus) -> Intake:
    return Intake(
        dynamodb=dynamodb, raw=RawStore(s3, RAW_BUCKET), bus=bus, component="api", env=ENV
    )


@pytest.fixture
def api(dynamodb: Any, intake: Intake, bus: RecordingBus) -> Api:
    return Api(dynamodb=dynamodb, intake=intake, bus=bus, clock=lambda: T0, env=ENV)


def request(
    method: str,
    resource: str,
    *,
    groups: str = "planner",
    body: Any = None,
    params: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": params,
        "queryStringParameters": query,
        "headers": headers or {},
        "body": None if body is None else json.dumps(body),
        "requestContext": {"authorizer": {"claims": {"sub": "u-1", "cognito:groups": groups}}},
    }


def body(response: dict[str, Any]) -> Any:
    return json.loads(response["body"])


def make_case(dynamodb: Any, n: int, rar: int, hours: float | None, status: CaseStatus) -> str:
    store = CaseStore(dynamodb, ENV)
    case_id = f"EXC-2026-{n:04d}"
    store.create(
        Case(
            case_id=case_id,
            type="MRP_EXCEPTION",
            material=f"MAT-{n:05d}",
            plant="1010",
            status=CaseStatus.RECEIVED,
            created_at=T0,
            updated_at=T0,
        ),
        actor="system",
    )
    stockout = None if hours is None else datetime.fromtimestamp(T0.timestamp() + hours * 3600, UTC)
    store.update_triage(
        case_id,
        rar_usd=Decimal(rar),
        stockout_at=stockout,
        priority_score=Decimal(rar),
        actor="system",
    )
    if status is not CaseStatus.RECEIVED:
        store.transition(case_id, CaseStatus.TRIAGED, actor="system")
    return case_id


def test_fr_tri_01_board_is_ranked_by_score_then_time_to_line_stop(api: Api, dynamodb: Any) -> None:
    make_case(dynamodb, 914, 4_720_000, 6.2, CaseStatus.TRIAGED)
    make_case(dynamodb, 915, 600_000, 45, CaseStatus.TRIAGED)
    make_case(dynamodb, 916, 180_000, 12, CaseStatus.TRIAGED)
    make_case(dynamodb, 917, 50_000, None, CaseStatus.RECEIVED)

    rows = body(api.handle(request("GET", "/cases")))

    assert [r["caseId"] for r in rows] == [
        "EXC-2026-0914",
        "EXC-2026-0915",
        "EXC-2026-0916",
        "EXC-2026-0917",
    ]
    assert rows[0]["stage"] == "TRIAGE" and rows[0]["rarUsd"] == 4_720_000
    only_new = body(api.handle(request("GET", "/cases", query={"status": "RECEIVED"})))
    assert [r["caseId"] for r in only_new] == ["EXC-2026-0917"]


def test_fr_rpt_02_metrics_endpoint_keeps_measured_basis(api: Api, dynamodb: Any) -> None:
    make_case(dynamodb, 914, 4_720_000, 6.2, CaseStatus.TRIAGED)

    values = body(api.handle(request("GET", "/metrics")))

    assert values["kpis"]["basis"] == "reference scenario (synthetic SAP Mirror)"
    assert values["kpis"]["caseCount"]["value"] == 1
    assert values["kpis"]["costPerCase"]["value"] is None
    assert api.handle(request("GET", "/metrics", groups="supplier"))["statusCode"] == 403


def test_at_27_decision_record_formats_and_access(
    api: Api, dynamodb: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id = make_case(dynamodb, 918, 10, None, CaseStatus.TRIAGED)
    monkeypatch.setattr("services.api.handler.collect", lambda *_: {"caseId": case_id})
    monkeypatch.setattr("services.api.handler.render_html", lambda *_: "<html>record</html>")
    monkeypatch.setattr("services.api.handler.render_pdf", lambda *_: b"%PDF-sample")
    route = "/cases/{id}/decision-record"

    def get(fmt: str | None = None, groups: str = "planner") -> dict[str, Any]:
        return api.handle(
            request(
                "GET",
                route,
                params={"id": case_id},
                groups=groups,
                query={"format": fmt} if fmt else None,
            )
        )

    assert body(get()) == {"caseId": case_id}
    html_result = get("html")
    assert html_result["headers"]["Content-Type"] == "text/html; charset=utf-8"
    assert html_result["body"] == "<html>record</html>"
    pdf = get("pdf")
    assert pdf["isBase64Encoded"] is True
    assert base64.b64decode(pdf["body"]) == b"%PDF-sample"
    assert get("other")["statusCode"] == 400
    assert get(groups="supplier")["statusCode"] == 403


def test_fr_neg_03_dialogue_thread_exposes_case_scoped_status(api: Api, dynamodb: Any) -> None:
    case_id = make_case(dynamodb, 914, 10, None, CaseStatus.RECEIVED)
    for number in (914, 915):
        dynamodb.put_item(
            TableName="aera-test-dialogue",
            Item=to_item(
                {
                    "PK": f"CASE#EXC-2026-{number:04d}",
                    "SK": "MSG#abc",
                    "messageId": "abc",
                    "templateId": "CONFIRM_SHIP_DATE",
                    "language": "DE",
                    "renderedText": "Lieferdatum?",
                    "englishCopy": "Delivery date?",
                    "status": "SENT",
                    "createdAt": T0,
                }
            ),
        )

    response = api.handle(request("GET", "/cases/{id}/dialogue", params={"id": case_id}))

    assert response["statusCode"] == 200
    assert body(response) == [
        {
            "messageId": "abc",
            "templateId": "CONFIRM_SHIP_DATE",
            "language": "DE",
            "renderedText": "Lieferdatum?",
            "englishCopy": "Delivery date?",
            "status": "SENT",
            "createdAt": T0.isoformat(),
        }
    ]
    assert (
        api.handle(
            request("GET", "/cases/{id}/dialogue", groups="supplier", params={"id": case_id})
        )["statusCode"]
        == 403
    )


def test_nfr_sec_04_roles_are_enforced(api: Api) -> None:
    assert api.handle(request("GET", "/cases", groups=""))["statusCode"] == 403
    assert api.handle(request("GET", "/cases", groups="approver"))["statusCode"] == 200
    upload = request("POST", "/signals", groups="approver", body={"sender": "x", "text": "y"})
    assert api.handle(upload)["statusCode"] == 403
    anonymous = request("GET", "/cases")
    anonymous["requestContext"] = {}
    assert api.handle(anonymous)["statusCode"] == 401


def test_errors_are_problem_json(api: Api) -> None:
    missing = api.handle(request("GET", "/cases/{id}", params={"id": "EXC-2026-9999"}))
    assert missing["statusCode"] == 404
    assert missing["headers"]["Content-Type"] == "application/problem+json"
    assert body(missing)["title"] == "Case not found"
    bad = api.handle(request("GET", "/cases", query={"status": "NOPE"}))
    assert bad["statusCode"] == 400


def test_case_detail_and_trace(api: Api, dynamodb: Any) -> None:
    case_id = make_case(dynamodb, 914, 1, 1, CaseStatus.TRIAGED)
    trace = TraceStore(dynamodb, ENV)
    first = TraceEvent(case_id=case_id, kind="SYSTEM", title="Case opened", ts=T0)
    second = TraceEvent(case_id=case_id, kind="TOOL_CALL", title="sap_get_purchase_order", ts=T0)
    trace.write(first)
    trace.write(second)

    detail = body(api.handle(request("GET", "/cases/{id}", params={"id": case_id})))
    events = body(api.handle(request("GET", "/cases/{id}/trace", params={"id": case_id})))
    later = body(
        api.handle(
            request(
                "GET",
                "/cases/{id}/trace",
                params={"id": case_id},
                query={"after": first.event_id},
            )
        )
    )

    assert detail["case"]["caseId"] == case_id and detail["signals"] == []
    assert [e["title"] for e in events] == ["Case opened", "sap_get_purchase_order"]
    assert [e["eventId"] for e in later] == [second.event_id]


def test_fr_ing_03_manual_upload_goes_through_the_same_intake(
    api: Api, bus: RecordingBus, dynamodb: Any
) -> None:
    upload = {
        "sender": "orders@krieger-guss.example",
        "text": "Scanned delivery note",
        "filename": "note.pdf",
        "contentType": "application/pdf",
        "contentBase64": base64.b64encode(b"%PDF-1.4 fake").decode(),
    }

    response = api.handle(request("POST", "/signals", body=upload))

    assert response["statusCode"] == 202
    signal = SignalStore(dynamodb, ENV).get(body(response)["signalId"])
    assert signal is not None and signal.channel is SignalChannel.MANUAL
    assert signal.attachments[0].endswith("att/01-note.pdf")
    assert bus.details("SignalReceived")[0]["actor"] == "user:u-1"


@pytest.mark.parametrize(
    ("upload", "status"),
    [
        ({"text": "no sender"}, 400),
        ({"sender": "a@b.example"}, 400),
        ({"sender": "a@b.example", "contentType": "text/html", "contentBase64": "PGgxPg=="}, 415),
        ({"sender": "a@b.example", "contentType": "image/png", "contentBase64": "@@"}, 400),
    ],
)
def test_bad_uploads_are_rejected(api: Api, upload: dict[str, Any], status: int) -> None:
    assert api.handle(request("POST", "/signals", body=upload))["statusCode"] == status


def test_idempotency_key_replays_the_first_response(api: Api, bus: RecordingBus) -> None:
    upload = {"sender": "orders@krieger-guss.example", "text": "hello"}
    headers = {"Idempotency-Key": "k-1"}

    first = api.handle(request("POST", "/signals", body=upload, headers=headers))
    second = api.handle(request("POST", "/signals", body=upload, headers=headers))

    assert first == second
    assert bus.types().count("SignalReceived") == 1


def test_br_02_planner_confirms_an_unconfirmed_field(
    api: Api, dynamodb: Any, intake: Intake, bus: RecordingBus
) -> None:
    case_id = make_case(dynamodb, 914, 1, 1, CaseStatus.TRIAGED)
    signal = intake.receive(
        Inbound(
            channel=SignalChannel.WHATSAPP,
            sender_id="+447700900234",
            body=b"{}",
            content_type="application/json",
        )
    )
    field = ExtractedField(
        field_id=f"{signal.signal_id}-01",
        signal_id=signal.signal_id,
        name="QUANTITY",
        value="640",
        confidence=0.71,
        status=FieldStatus.UNCONFIRMED,
    )
    SignalStore(dynamodb, ENV).save(
        signal.model_copy(
            update={"status": SignalStatus.ACCEPTED, "case_id": case_id, "fields": [field]}
        )
    )

    response = api.handle(
        request(
            "POST",
            "/cases/{id}/fields/{fieldId}/confirm",
            params={"id": case_id, "fieldId": field.field_id},
            body={"value": "640"},
        )
    )

    assert response["statusCode"] == 200
    assert body(response)["status"] == "CONFIRMED" and body(response)["confirmedBy"] == "user:u-1"
    stored = SignalStore(dynamodb, ENV).get(signal.signal_id)
    assert stored is not None and stored.fields[0].status is FieldStatus.CONFIRMED
    events = AuditWriter(dynamodb, ENV).events(f"CASE#{case_id}")
    assert events[-1].type == "FIELD_CONFIRMED" and events[-1].actor == "user:u-1"


def test_fr_ui_05_quarantined_signals_are_listed_with_their_reason(
    api: Api, dynamodb: Any, intake: Intake
) -> None:
    signal = intake.receive(
        Inbound(
            channel=SignalChannel.EMAIL,
            sender_id="orders@kreiger-guss.example",
            body=b"x",
            content_type="message/rfc822",
        )
    )
    SignalStore(dynamodb, ENV).save(
        signal.model_copy(
            update={"status": SignalStatus.QUARANTINED, "quarantine_reason": "look-alike domain"}
        )
    )

    rows = body(api.handle(request("GET", "/signals")))

    assert [(r["signalId"], r["quarantineReason"]) for r in rows] == [
        (signal.signal_id, "look-alike domain")
    ]


def test_realtime_ticket_is_short_lived_and_random(api: Api, dynamodb: Any) -> None:
    first = body(api.handle(request("POST", "/realtime/ticket", groups="approver", body={})))
    second = body(api.handle(request("POST", "/realtime/ticket", groups="approver", body={})))

    assert first["expiresIn"] == 60 and first["ticket"] != second["ticket"]
    item = dynamodb.get_item(
        TableName="aera-test-connections", Key={"PK": {"S": f"TICKET#{first['ticket']}"}}
    )["Item"]
    assert item["userId"]["S"] == "u-1"


def test_metrics_carry_the_mrp_header_counts(api: Api, dynamodb: Any) -> None:
    dynamodb.put_item(
        TableName="aera-test-cases",
        Item={
            "PK": {"S": "BOARD"},
            "SK": {"S": "MRP"},
            "total": {"N": "214"},
            "actionable": {"N": "6"},
            "suppressed": {"N": "208"},
        },
    )
    assert body(api.handle(request("GET", "/metrics")))["mrp"] == {
        "total": 214,
        "actionable": 6,
        "suppressed": 208,
    }


def test_uc_05_answering_the_last_question_makes_the_case_ready_for_a_new_run(
    api: Api, dynamodb: Any, intake: Intake, bus: RecordingBus
) -> None:
    case_id = make_case(dynamodb, 914, 1, 1, CaseStatus.TRIAGED)
    store = CaseStore(dynamodb, ENV)
    store.transition(case_id, CaseStatus.INVESTIGATING, actor="system")
    store.transition(case_id, CaseStatus.WAITING_PLANNER, actor="agent")
    signal = intake.receive(
        Inbound(
            channel=SignalChannel.WHATSAPP,
            sender_id="+447700900234",
            body=b"{}",
            content_type="application/json",
        )
    )
    field = ExtractedField(
        field_id=f"{signal.signal_id}-01",
        signal_id=signal.signal_id,
        name="QUANTITY",
        value="640",
        confidence=0.71,
        status=FieldStatus.UNCONFIRMED,
    )
    SignalStore(dynamodb, ENV).save(
        signal.model_copy(
            update={"status": SignalStatus.ACCEPTED, "case_id": case_id, "fields": [field]}
        )
    )
    dynamodb.put_item(
        TableName="aera-test-cases",
        Item={
            "PK": {"S": f"CASE#{case_id}"},
            "SK": {"S": "QUESTION#01"},
            "status": {"S": "OPEN"},
            "fieldId": {"S": field.field_id},
        },
    )

    api.handle(
        request(
            "POST",
            "/cases/{id}/fields/{fieldId}/confirm",
            params={"id": case_id, "fieldId": field.field_id},
            body={},
        )
    )

    [ready] = bus.details("CaseReadyForRun")
    assert ready["data"] == {"caseId": case_id, "reason": "planner answered"}
    question = dynamodb.get_item(
        TableName="aera-test-cases",
        Key={"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": "QUESTION#01"}},
    )["Item"]
    assert question["status"]["S"] == "ANSWERED" and question["answeredBy"]["S"] == "user:u-1"


def test_fr_tri_03_board_rows_explain_their_rank(api: Api, dynamodb: Any) -> None:
    make_case(dynamodb, 914, 4_720_000, 6.2, CaseStatus.TRIAGED)
    make_case(dynamodb, 915, 600_000, 45, CaseStatus.TRIAGED)

    rows = body(api.handle(request("GET", "/cases")))

    assert rows[0]["rankReason"].startswith("EXC-2026-0914 ranks above EXC-2026-0915")
    assert "rankReason" not in rows[1]


def test_nfr_rel_04_post_runs_returns_a_run_id_at_once(
    api: Api, dynamodb: Any, bus: RecordingBus
) -> None:
    case_id = make_case(dynamodb, 914, 1, 1, CaseStatus.TRIAGED)

    response = api.handle(request("POST", "/cases/{id}/runs", params={"id": case_id}, body={}))

    assert response["statusCode"] == 202
    run_id = body(response)["runId"]
    [ready] = bus.details("CaseReadyForRun")
    assert ready["data"] == {"caseId": case_id, "reason": "planner request", "runId": run_id}
    assert ready["actor"] == "user:u-1"


def test_runs_are_refused_for_cases_that_cannot_start(api: Api, dynamodb: Any) -> None:
    case_id = make_case(dynamodb, 914, 1, 1, CaseStatus.RECEIVED)
    response = api.handle(request("POST", "/cases/{id}/runs", params={"id": case_id}, body={}))
    assert response["statusCode"] == 409


def test_nfr_rel_04_replayed_api_request_emits_one_run_id(
    api: Api, dynamodb: Any, bus: RecordingBus
) -> None:
    case_id = make_case(dynamodb, 914, 1, 1, CaseStatus.TRIAGED)
    event = request(
        "POST",
        "/cases/{id}/runs",
        params={"id": case_id},
        body={},
        headers={"Idempotency-Key": "start-reference-run"},
    )
    first = api.handle(event)
    repeated = api.handle(event)
    assert first["statusCode"] == repeated["statusCode"] == 202
    assert body(first) == body(repeated)
    assert len(bus.details("CaseReadyForRun")) == 1


def test_nfr_rel_04_active_run_refuses_another_start(
    api: Api, dynamodb: Any, bus: RecordingBus
) -> None:
    case_id = make_case(dynamodb, 914, 1, 1, CaseStatus.TRIAGED)
    dynamodb.update_item(
        TableName="aera-test-cases",
        Key={"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": "META"}},
        UpdateExpression="SET activeRunId = :run",
        ExpressionAttributeValues={":run": {"S": "already-running"}},
    )
    response = api.handle(request("POST", "/cases/{id}/runs", params={"id": case_id}, body={}))
    assert response["statusCode"] == 409
    assert not bus.details("CaseReadyForRun")


class States:
    class exceptions:  # noqa: N801 - mirrors boto3's client.exceptions namespace
        ExecutionAlreadyExists = type("ExecutionAlreadyExists", (Exception,), {})

    def __init__(self) -> None:
        self.started: dict[str, dict[str, Any]] = {}

    def start_execution(self, **request: Any) -> dict[str, Any]:
        if request["name"] in self.started:
            raise self.exceptions.ExecutionAlreadyExists()
        self.started[request["name"]] = request
        return {}


def reopened_case(dynamodb: Any) -> str:
    case_id = make_case(dynamodb, 914, 1, 1, CaseStatus.TRIAGED)
    store = CaseStore(dynamodb, ENV)
    for status in (
        CaseStatus.INVESTIGATING,
        CaseStatus.PLAN_PROPOSED,
        CaseStatus.VERIFIED,
        CaseStatus.AUTO_APPROVED,
        CaseStatus.EXECUTING,
        CaseStatus.MONITORING,
        CaseStatus.REOPENED,
    ):
        store.transition(case_id, status, actor="system")
    return case_id


def test_fr_exe_08_approver_rollback_is_audited_and_idempotent(api: Api, dynamodb: Any) -> None:
    states = States()
    api.states, api.execution_arn = (
        states,
        "arn:aws:states:us-east-1:0:stateMachine:aera-test-execution",
    )
    case_id = reopened_case(dynamodb)
    call = request(
        "POST", "/cases/{id}/rollback", groups="approver", params={"id": case_id}, body={}
    )

    first = api.handle(call)
    second = api.handle(call)

    assert first["statusCode"] == second["statusCode"] == 202
    [started] = states.started.values()
    assert json.loads(started["input"]) == {"mode": "rollback", "caseId": case_id}
    events = AuditWriter(dynamodb, ENV).events(f"CASE#{case_id}")
    assert [e.type for e in events].count("ROLLBACK_REQUESTED") == 1


def test_rollback_needs_an_approver_and_a_reopened_case(api: Api, dynamodb: Any) -> None:
    api.states, api.execution_arn = States(), "arn"
    case_id = make_case(dynamodb, 914, 1, 1, CaseStatus.TRIAGED)
    planner = request("POST", "/cases/{id}/rollback", params={"id": case_id}, body={})
    approver = request(
        "POST", "/cases/{id}/rollback", groups="approver", params={"id": case_id}, body={}
    )

    assert api.handle(planner)["statusCode"] == 403
    assert api.handle(approver)["statusCode"] == 409
