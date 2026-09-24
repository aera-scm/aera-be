"""FR-LAB-04 / AT-25 offline path: synthetic runs remain inspectable."""

import json
from datetime import UTC, datetime
from typing import Any

from services.api.handler import Api
from services.conftest import RAW_BUCKET, RecordingBus
from services.lab.service import Lab
from services.reporting.metrics import kpis
from services.shared.cases import CaseStore
from services.shared.intake import Intake
from services.shared.models import Case, CaseStatus, SignalStatus
from services.shared.signals import RawStore, SignalStore

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)
PARAMS = {
    "exceptionType": "SUPPLIER_DELAY",
    "material": "MAT-51002",
    "plant": "1010",
    "daysLate": 3,
    "quantityShort": 200,
    "channel": "WHATSAPP",
    "language": "DE",
    "hostile": True,
}


def request(
    method: str, resource: str, groups: str, body: Any = None, run_id: str = ""
) -> dict[str, Any]:
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": {"id": run_id} if run_id else None,
        "headers": {},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": {"sub": "u-1", "cognito:groups": groups}}},
    }


def test_fr_lab_04_run_reaches_gate_and_records_case_outcome(
    dynamodb: Any, s3: Any, bus: RecordingBus
) -> None:
    patches: list[list[dict[str, object]]] = []
    intake = Intake(
        dynamodb=dynamodb, raw=RawStore(s3, RAW_BUCKET), bus=bus, component="api", env="test"
    )
    lab = Lab(dynamodb, intake, patches.append, clock=lambda: NOW, env="test")
    api = Api(dynamodb=dynamodb, intake=intake, bus=bus, env="test", lab=lab)
    denied = api.handle(request("POST", "/lab/runs", "planner", PARAMS))
    invalid = api.handle(request("POST", "/lab/runs", "admin", {**PARAMS, "plant": "9999"}))
    submitted = api.handle(request("POST", "/lab/runs", "admin", PARAMS))

    assert denied["statusCode"] == 403 and invalid["statusCode"] == 400
    assert submitted["statusCode"] == 202
    row = json.loads(submitted["body"])
    assert row["synthetic"] is True and row["deliveryMode"] == "internal-replay"
    assert row["parameters"] == PARAMS and row["outcome"] == "IN_PROGRESS"
    assert patches[0][-1]["upsert"] is True
    assert len(row["signalIds"]) == 2 and bus.types() == ["SignalReceived", "SignalReceived"]
    signals = SignalStore(dynamodb, "test")
    main = signals.get(row["signalIds"][0])
    hostile = signals.get(row["signalIds"][1])
    assert main and main.attachments and main.channel.value == "WHATSAPP"
    assert hostile and hostile.channel.value == "EMAIL"

    case_id = "EXC-2026-0914"
    CaseStore(dynamodb, "test").create(
        Case(
            case_id=case_id,
            type="SUPPLIER_DELAY",
            material="MAT-51002",
            plant="1010",
            status=CaseStatus.CLOSED,
            created_at=NOW,
            updated_at=NOW,
        ),
        actor="system",
    )
    signals.save(main.model_copy(update={"status": SignalStatus.ACCEPTED, "case_id": case_id}))
    waiting = json.loads(
        api.handle(request("GET", "/lab/runs/{id}", "admin", run_id=row["runId"]))["body"]
    )
    assert waiting["outcome"] == "IN_PROGRESS" and waiting["hostileBlocked"] is False
    # A terminal case must wait for the requested hostile receipt to enter the gate.
    lab._save(
        row["runId"],
        {
            "receiptKeys": [
                {"channel": "WHATSAPP", "dedupKey": f"lab-{row['runId']}-photo"},
                {"channel": "EMAIL", "dedupKey": "missing-hostile"},
            ]
        },
    )
    assert lab.get(row["runId"])["outcome"] == "IN_PROGRESS"
    lab._save(row["runId"], {"receiptKeys": [], "signalIds": row["signalIds"]})
    signals.save(hostile.model_copy(update={"status": SignalStatus.ACCEPTED}))
    unsafe = json.loads(
        api.handle(request("GET", "/lab/runs/{id}", "admin", run_id=row["runId"]))["body"]
    )
    assert unsafe["outcome"] == "HOSTILE_NOT_BLOCKED"
    signals.save(hostile.model_copy(update={"status": SignalStatus.QUARANTINED}))
    result = api.handle(request("GET", "/lab/runs/{id}", "admin", run_id=row["runId"]))
    final = json.loads(result["body"])
    assert final["outcome"] == "RESOLVED" and final["hostileBlocked"] is True
    assert final["caseId"] == case_id
    assert lab.get(row["runId"])["outcome"] == "RESOLVED"
    events = lab.audit.events(f"LAB#{row['runId']}")
    outcomes = [event for event in events if event.type == "LAB_OUTCOME"]
    assert len(outcomes) == 2
    listed = json.loads(api.handle(request("GET", "/lab/runs", "admin"))["body"])
    assert listed[0]["outcome"] == "RESOLVED"
    assert (
        kpis(dynamodb, "test")["basis"] == "reference scenario and Lab runs (synthetic SAP Mirror)"
    )


def test_fr_lab_04_failed_mutation_never_submits_signal(
    dynamodb: Any, s3: Any, bus: RecordingBus
) -> None:
    intake = Intake(
        dynamodb=dynamodb, raw=RawStore(s3, RAW_BUCKET), bus=bus, component="api", env="test"
    )

    def broken(_: list[dict[str, object]]) -> None:
        raise RuntimeError("Mirror unavailable")

    lab = Lab(dynamodb, intake, broken, clock=lambda: NOW, env="test")
    api = Api(dynamodb=dynamodb, intake=intake, bus=bus, env="test", lab=lab)
    refused = api.handle(request("POST", "/lab/runs", "admin", PARAMS))
    rows = json.loads(api.handle(request("GET", "/lab/runs", "admin"))["body"])
    assert refused["statusCode"] == 503
    assert rows[0]["status"] == "FAILED" and rows[0]["outcome"] == "DELIVERY_FAILED"
    assert bus.types() == []


def test_fr_lab_01_selected_plant_uses_matching_po_in_signal(
    dynamodb: Any, s3: Any, bus: RecordingBus
) -> None:
    intake = Intake(
        dynamodb=dynamodb, raw=RawStore(s3, RAW_BUCKET), bus=bus, component="api", env="test"
    )
    lab = Lab(dynamodb, intake, lambda _: None, clock=lambda: NOW, env="test")
    row = lab.start({**PARAMS, "plant": "1030", "hostile": False}, "user:admin")
    signal = SignalStore(dynamodb, "test").get(row["signalIds"][0])
    assert row["poNumber"] == "4502001240"
    assert signal is not None and signal.po_number == row["poNumber"]
