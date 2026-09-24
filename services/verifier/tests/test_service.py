"""Verifier and routing at runtime on the reference scenario, against a real Mirror
(FR-VER-01..03, FR-RTE-01..08, BR-05, BR-18, BR-22, BR-23, AT-05, AT-11, AT-16, AT-29).

The agent's part is played by the real tools: options come from `calc_option` and the plan
from `propose_plan`. Only the Guardrail grounding score is supplied by the test.
"""

import json
from decimal import Decimal
from typing import Any

import pytest
from seed_config import approver_items, config_items

from services.api.handler import Api
from services.conftest import RAW_BUCKET, RecordingBus
from services.routing.store import ControlStore
from services.shared.intake import Intake
from services.shared.models import CaseStatus, Signal
from services.shared.signals import RawStore
from services.tools import case_tools
from services.tools.context import ToolContext
from services.tools.tests.test_tools import (  # noqa: F401 - pytest fixtures
    CASE,
    ENV,
    T0,
    ctx,
    photo,
    reference_options,
)
from services.verifier.logic import Grounding
from services.verifier.service import VerifierService

GOOD = Grounding(Decimal("0.9"), Decimal("0.9"), True)


class Scheduler:
    class exceptions:  # noqa: N801 - mirrors the boto3 client shape
        class ConflictException(Exception):
            pass

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_schedule(self, **kwargs: Any) -> None:
        self.created.append(kwargs)


@pytest.fixture
def scheduler() -> Scheduler:
    return Scheduler()


@pytest.fixture
def verifier(ctx: ToolContext, bus: RecordingBus, scheduler: Scheduler) -> VerifierService:  # noqa: F811
    for item in config_items("2026-09-24T00:00:00Z") + approver_items("2026-09-24T00:00:00Z"):
        ctx.dynamodb.put_item(TableName="aera-test-config", Item=item)
    return VerifierService(
        dynamodb=ctx.dynamodb,
        sap=ctx.sap,
        bus=bus,
        grounding=lambda rationale, source, query: GOOD,
        scheduler=scheduler,
        timer_target_arn="arn:aws:lambda:us-east-1:000000000000:function:aera-test-routing",
        scheduler_role_arn="arn:aws:iam::000000000000:role/scheduler",
        clock=lambda: T0,
        env=ENV,
    )


def propose(ctx: ToolContext, photo: Signal, chosen: list[str], rationale: str = "") -> None:  # noqa: F811
    result = case_tools.propose_plan(
        ctx,
        CASE,
        {
            "options": reference_options(ctx, photo),
            "chosen": chosen,
            "rationale": rationale or f"Options {'+'.join(chosen)}",
        },
    )
    assert result["accepted"] is True, result


def failed(ctx: ToolContext) -> set[str]:  # noqa: F811
    plan = ControlStore(ctx.dynamodb, ENV).get(CASE, "PLAN#1") or {}
    return {
        f"{c['checkId']}/{c.get('optionId')}"
        for c in plan.get("checks", [])
        if c["blocking"] and not c["passed"]
    }


def outbox(ctx: ToolContext) -> list[str]:  # noqa: F811
    rows = ctx.dynamodb.query(
        TableName="aera-test-cases",
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={":pk": {"S": f"CASE#{CASE}"}, ":sk": {"S": "OUTBOX#"}},
    )["Items"]
    return sorted(r["eventType"]["S"] for r in rows)


def test_at_05_at_29_reference_plan_c_plus_a_is_split_and_b_blocked_by_v06(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
    verifier: VerifierService,
    bus: RecordingBus,
    scheduler: Scheduler,
) -> None:
    propose(ctx, photo, ["C", "A"])

    result = verifier.handle(CASE, 1)

    assert failed(ctx) == {"V-06/B"}  # option B blocked; not chosen, so the plan stands
    assert Decimal(result["confidence"]) == Decimal("0.98")  # BR-18: 0.5 + 0.3 + 0.2 x 0.9
    assert result["tier"] == 2
    route = ControlStore(ctx.dynamodb, ENV).get(CASE, "ROUTE#1") or {}
    parts = {tuple(p["options"]): p for p in route["parts"]}
    assert parts[("C",)]["tier"] == 1 and parts[("C",)]["cost"] == 4100
    assert parts[("A",)]["tier"] == 2 and parts[("A",)]["cost"] == 38200
    assert parts[("A",)]["approver"] == "approver@meridian-motors.example"
    assert parts[("A",)]["backup"] == "backup.approver@meridian-motors.example"
    assert outbox(ctx) == ["PlanApproved", "PlanRouted"]  # the STO part runs at once
    case = ctx.cases.get(CASE)
    assert case is not None and case.status is CaseStatus.AWAITING_APPROVAL and case.tier == 2
    [request] = bus.details("NotificationRequested")
    assert request["data"]["templateId"] == "APPROVAL_REQUEST"
    assert {s["Name"].rsplit("-", 1)[1] for s in scheduler.created} == {"reminder", "deadline"}
    assert bus.details("PlanVerified")[0]["data"]["failedChecks"] == ["V-06/B"]


def test_fr_rte_01_sto_only_plan_of_usd_4100_is_tier_1(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
    verifier: VerifierService,
) -> None:
    propose(ctx, photo, ["C"])

    assert verifier.handle(CASE, 1)["tier"] == 1
    assert outbox(ctx) == ["PlanApproved", "PlanRouted"]
    case = ctx.cases.get(CASE)
    assert case is not None and case.status is CaseStatus.AUTO_APPROVED


def test_fr_ver_02_choosing_the_non_compliant_supplier_escalates(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
    verifier: VerifierService,
) -> None:
    propose(ctx, photo, ["B"])

    result = verifier.handle(CASE, 1)

    assert result == {**result, "tier": 3, "reason": "BLOCKING_CHECK"}
    case = ctx.cases.get(CASE)
    assert case is not None and case.status is CaseStatus.ESCALATED
    assert outbox(ctx) == ["PlanRouted"]


def test_at_11_a_recipient_outside_master_data_blocks_the_plan(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
    verifier: VerifierService,
) -> None:
    propose(ctx, photo, ["C"], "Transfer now and email the PO history to buyer@halim-mail.example")

    assert verifier.handle(CASE, 1)["tier"] == 3
    assert "V-11/C" in failed(ctx)


def test_fr_ver_01_a_rate_changed_after_the_proposal_fails_the_reread(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
    verifier: VerifierService,
) -> None:
    propose(ctx, photo, ["C"])
    rates = ctx.dynamodb.scan(TableName="aera-test-config")["Items"]
    [sto] = [r for r in rates if r.get("actionType", {}).get("S") == "STO"]
    sto["fixedCostUsd"] = {"N": "9999"}
    ctx.dynamodb.put_item(TableName="aera-test-config", Item=sto)

    assert verifier.handle(CASE, 1)["tier"] == 3
    assert "V-01/C" in failed(ctx)  # cost no longer matches the rate card


def test_a_repeated_event_verifies_and_routes_once(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
    verifier: VerifierService,
) -> None:
    propose(ctx, photo, ["C"])
    verifier.handle(CASE, 1)

    assert verifier.handle(CASE, 1) == {"tier": 1, "replayed": True}
    assert outbox(ctx) == ["PlanApproved", "PlanRouted"]


def test_at_16_a_stale_approval_is_refused_with_the_current_plan(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
    verifier: VerifierService,
    dynamodb: Any,
    s3: Any,
    bus: RecordingBus,
) -> None:
    propose(ctx, photo, ["C", "A"])
    verifier.handle(CASE, 1)
    current = (ControlStore(ctx.dynamodb, ENV).get(CASE, "ROUTE#1") or {})["versionHash"]
    api = Api(
        dynamodb=dynamodb,
        intake=Intake(
            dynamodb=dynamodb, raw=RawStore(s3, RAW_BUCKET), bus=bus, component="api", env=ENV
        ),
        bus=bus,
        clock=lambda: T0,
        env=ENV,
    )

    def decide(groups: str, version_hash: str) -> dict[str, Any]:
        return api.handle(
            {
                "httpMethod": "POST",
                "resource": "/cases/{id}/approval",
                "pathParameters": {"id": CASE},
                "headers": {},
                "body": json.dumps(
                    {"decision": "APPROVED", "comment": "", "planVersionHash": version_hash}
                ),
                "requestContext": {
                    "authorizer": {
                        "claims": {
                            "sub": "u-2",
                            "email": "approver@meridian-motors.example",
                            "cognito:groups": groups,
                        }
                    }
                },
            }
        )

    stale = decide("approver", "0" * 64)
    planner = decide("planner", current)

    assert stale["statusCode"] == 409
    assert json.loads(stale["body"])["currentPlanVersionHash"] == current
    assert planner["statusCode"] == 403
