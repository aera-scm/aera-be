"""Case detail carries the current plan, route and execution evidence (SRD 6.10,
FR-UI-07, FR-UI-08, FR-UI-10, FR-RTE-02, FR-RTE-03, FR-RTE-07, FR-RTE-08, FR-VER-02, AT-16)."""

from datetime import timedelta
from typing import Any

import pytest

from services.api.handler import Api
from services.api.tests.test_api import ENV, T0, body, make_case, request
from services.conftest import RAW_BUCKET, RecordingBus
from services.execution.journal import idempotency_key
from services.routing.logic import Route, route
from services.routing.store import ControlStore
from services.shared.audit import AuditWriter
from services.shared.dynamo import table_name, to_item
from services.shared.intake import Intake
from services.shared.models import CaseDetail, CaseStatus, json_schemas
from services.shared.signals import RawStore
from services.verifier.tests.test_verification import NOW, limits, verified

CASE = "EXC-2026-0914"


@pytest.fixture
def api(dynamodb: Any, s3: Any, bus: RecordingBus) -> Api:
    intake = Intake(
        dynamodb=dynamodb, raw=RawStore(s3, RAW_BUCKET), bus=bus, component="api", env=ENV
    )
    return Api(dynamodb=dynamodb, intake=intake, bus=bus, clock=lambda: T0, env=ENV)


def seed_routed(dynamodb: Any, stockout_hours: float = 6.2) -> Route:
    """The reference plan C + A, verified, stored and routed as the verifier does."""
    make_case(dynamodb, 914, 4_720_000, 6.2, CaseStatus.TRIAGED)
    cases = table_name("cases", ENV)
    dynamodb.update_item(
        TableName=cases,
        Key=to_item({"PK": f"CASE#{CASE}", "SK": "META"}),
        UpdateExpression="SET planVersion = :v",
        ExpressionAttributeValues=to_item({":v": 1}),
    )
    config = table_name("config", ENV)
    for limit in limits():
        dynamodb.put_item(
            TableName=config,
            Item=to_item({"PK": f"APPR#{limit.user_id}", **limit.model_dump(by_alias=True)}),
        )
    dynamodb.put_item(TableName=config, Item=to_item({"PK": "CFG#KILL_SWITCH", "value": "off"}))
    value = verified()
    dynamodb.put_item(
        TableName=cases,
        Item=to_item(
            {
                "PK": f"CASE#{CASE}",
                "SK": "PLAN#1",
                **value.record.model_dump(mode="json", by_alias=True),
                "projection": {"options": {}},
                "automatedReasoning": {
                    "status": "CONSISTENT",
                    "statements": "internal policy statements",
                    "findings": ["SATISFIABLE"],
                    "policyArn": "test-policy",
                },
            }
        ),
    )
    result = route(
        value,
        now=NOW,
        stockout=NOW + timedelta(hours=stockout_hours),
        plant="1010",
        limits=limits(),
    )
    ControlStore(dynamodb, ENV).save(value, result, plant="1010", now=NOW)
    return result


def detail(api: Api, groups: str = "approver") -> dict[str, Any]:
    response = api.handle(request("GET", "/cases/{id}", params={"id": CASE}, groups=groups))
    assert response["statusCode"] == 200
    value: dict[str, Any] = body(response)
    return value


def test_FR_RTE_02_FR_UI_07_case_detail_shows_plan_checks_confidence_and_hash(
    api: Api, dynamodb: Any
) -> None:
    result = seed_routed(dynamodb)

    plan = detail(api)["plan"]

    assert plan["plan"]["chosen"] == ["C", "A"]
    assert [o["id"] for o in plan["plan"]["options"]] == ["A", "B", "C"]
    assert all(f["sourceRef"] for o in plan["plan"]["options"] for f in o["figures"])
    assert plan["confidence"] is not None
    assert plan["checks"]
    assert plan["planVersionHash"] == result.version_hash
    assert plan["automatedReasoning"] == {"status": "CONSISTENT", "findings": ["SATISFIABLE"]}
    assert "projection" not in plan


def test_FR_VER_02_blocked_option_shows_failing_check_and_reason(api: Api, dynamodb: Any) -> None:
    seed_routed(dynamodb)

    checks = detail(api)["plan"]["checks"]

    failed = [c for c in checks if c["optionId"] == "B" and not c["passed"] and c["blocking"]]
    assert [c["checkId"] for c in failed] == ["V-06"]
    assert ":" in failed[0]["detail"]  # rule text followed by the reason


def test_FR_RTE_08_FR_RTE_07_both_parts_with_approver_deadline_and_reminder(
    api: Api, dynamodb: Any
) -> None:
    result = seed_routed(dynamodb)

    view = detail(api)["route"]

    assert view["tier"] == 2
    assert view["planVersion"] == 1
    assert view["planVersionHash"] == result.version_hash
    parts = view["parts"]
    assert [(p["options"], p["tier"], p["costUsd"]) for p in parts] == [
        (["C"], 1, 4100),
        (["A"], 2, 38200),
    ]
    pending = parts[1]
    stored = result.parts[1]
    assert pending["planPartId"] == stored.id
    assert (pending["approverId"], pending["backupApproverId"]) == ("primary", "backup")
    assert stored.deadline is not None and stored.reminder is not None
    assert pending["deadlineAt"] == stored.deadline.isoformat().replace("+00:00", "Z")
    assert pending["reminderAt"] == stored.reminder.isoformat().replace("+00:00", "Z")
    assert (pending["reminded"], pending["expired"], pending["decision"]) == (False, False, None)


def test_FR_RTE_02_BR_17_undo_summary_per_action_before_execution(api: Api, dynamodb: Any) -> None:
    seed_routed(dynamodb)

    sto, air = detail(api)["route"]["parts"]

    assert sto["undoSummary"] == [
        {
            "optionId": "C",
            "actionType": "CREATE_STO",
            "reversible": True,
            "undo": "Set the deletion indicator on the STO item before goods issue.",
        }
    ]
    assert air["undoSummary"] == [
        {
            "optionId": "A",
            "actionType": "BOOK_AIR_FREIGHT",
            "reversible": False,
            "undo": "Not reversible once booked; the freight cost stays.",
        }
    ]


def test_FR_RTE_03_decision_and_comment_are_shown_after_approval(api: Api, dynamodb: Any) -> None:
    result = seed_routed(dynamodb, stockout_hours=24)
    pending = next(p for p in result.parts if p.tier == 2)
    ControlStore(dynamodb, ENV).decide(
        CASE,
        actor="primary",
        groups=frozenset({"approver"}),
        version_hash=result.version_hash,
        decision="APPROVED",
        comment="Checked sources",
        now=NOW,
    )

    part = next(p for p in detail(api)["route"]["parts"] if p["planPartId"] == pending.id)

    assert (part["decision"], part["comment"]) == ("APPROVED", "Checked sources")
    assert part["decidedAt"] == NOW.isoformat().replace("+00:00", "Z")


def test_FR_RTE_07_expired_part_moves_to_backup(api: Api, dynamodb: Any) -> None:
    result = seed_routed(dynamodb, stockout_hours=24)
    pending = next(p for p in result.parts if p.tier == 2)
    assert pending.deadline is not None
    ControlStore(dynamodb, ENV).tick(CASE, pending.id, now=pending.deadline)

    part = next(p for p in detail(api)["route"]["parts"] if p["planPartId"] == pending.id)

    assert part["expired"] is True
    assert part["approverId"] == "backup"


def test_FR_UI_10_execution_steps_sap_documents_and_milestones(api: Api, dynamodb: Any) -> None:
    result = seed_routed(dynamodb)
    sto = result.parts[0]
    idempotency = table_name("idempotency", ENV)
    action = {
        "type": "CREATE_STO",
        "fromPlant": "1020",
        "toPlant": "1010",
        "material": "M1",
        "qty": 600,
        "deliveryDate": NOW.date().isoformat(),
    }
    step_key = idempotency_key(CASE, 1, 2, "CREATE_STO", "1020>1010:M1")
    dynamodb.put_item(
        TableName=idempotency,
        Item=to_item(
            {
                "PK": f"EXEC#{CASE}#1#{sto.id}",
                "request": {
                    "versionHash": result.version_hash,
                    "steps": [
                        {
                            "index": 2,
                            "target": "1020>1010:M1",
                            "action": action,
                            "sourceRefs": ["SAP:stock"],
                        }
                    ],
                },
                "undo": {"type": "REVERSE_COMPLETED_ACTIONS"},
                "status": "SUCCEEDED",
                "result": {"results": [{"document": "4500000123", "sourceRef": "SAP:sto"}]},
            }
        ),
    )
    dynamodb.put_item(
        TableName=idempotency,
        Item=to_item(
            {
                "PK": step_key,
                "request": {"action": action, "target": "1020>1010:M1"},
                "undo": {"type": "DELETE_STO_ITEM", "beforeGoodsIssue": True},
                "status": "SUCCEEDED",
                "result": {"document": "4500000123", "sourceRef": "SAP:sto"},
            }
        ),
    )
    audit = AuditWriter(dynamodb, ENV)
    for kind in ("UNDO_SAVED", "EXECUTION_COMPLETED"):
        audit.record(f"CASE#{CASE}", kind, actor="system", case_id=CASE, payload={"partId": sto.id})
    audit.record(
        f"CASE#{CASE}", "UNDO_SAVED", actor="system", case_id=CASE, payload={"partId": "other"}
    )

    executions = detail(api)["execution"]

    assert len(executions) == 1
    run = executions[0]
    assert (run["planPartId"], run["status"]) == (sto.id, "SUCCEEDED")
    assert run["steps"] == [
        {
            "index": 2,
            "actionType": "CREATE_STO",
            "target": "1020>1010:M1",
            "status": "SUCCEEDED",
            "sapDocument": "4500000123",
            "sourceRef": "SAP:sto",
            "undoType": "DELETE_STO_ITEM",
            "irreversible": False,
        }
    ]
    assert [m["type"] for m in run["milestones"]] == ["UNDO_SAVED", "EXECUTION_COMPLETED"]


def test_case_detail_without_plan_invents_no_records(api: Api, dynamodb: Any) -> None:
    make_case(dynamodb, 914, 1, 1, CaseStatus.TRIAGED)

    value = detail(api, groups="planner")

    assert (value["plan"], value["route"], value["execution"]) == (None, None, [])


def test_FR_RTE_03_route_of_an_older_plan_version_is_not_shown(api: Api, dynamodb: Any) -> None:
    seed_routed(dynamodb)
    dynamodb.update_item(
        TableName=table_name("cases", ENV),
        Key=to_item({"PK": f"CASE#{CASE}", "SK": "META"}),
        UpdateExpression="SET planVersion = :v",
        ExpressionAttributeValues=to_item({":v": 2}),
    )

    value = detail(api)

    assert (value["plan"], value["route"], value["execution"]) == (None, None, [])


def test_AT_16_stale_approval_is_problem_json_with_the_current_hash(
    api: Api, dynamodb: Any
) -> None:
    result = seed_routed(dynamodb, stockout_hours=24)
    event = request(
        "POST",
        "/cases/{id}/approval",
        params={"id": CASE},
        groups="approver",
        body={"decision": "APPROVED", "comment": "", "planVersionHash": "stale"},
    )
    event["requestContext"]["authorizer"]["claims"]["email"] = "primary"

    response = api.handle(event)

    assert response["statusCode"] == 409
    assert response["headers"]["Content-Type"] == "application/problem+json"
    problem = body(response)
    assert (problem["status"], problem["title"]) == (409, "Decision refused")
    assert problem["currentPlanVersion"] == 1
    assert problem["currentPlanVersionHash"] == result.version_hash


def test_SRD_6_10_case_detail_matches_the_exported_contract(api: Api, dynamodb: Any) -> None:
    seed_routed(dynamodb)

    payload = detail(api)
    payload["case"].pop("stage")  # computed on serialisation, not an input field

    CaseDetail.model_validate(payload)

    assert "CaseDetail" in json_schemas()
