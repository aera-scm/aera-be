"""Projection and what-if endpoints (FR-SIM-01..03, AT-19): new projection and checks for an
edited option; the plan of record, its drafts and its route stay unchanged."""

import json
from datetime import timedelta
from typing import Any

import pytest

from services.api.handler import Api
from services.conftest import RAW_BUCKET, RecordingBus
from services.shared.intake import Intake
from services.shared.models import Signal
from services.shared.signals import RawStore
from services.tools import calc, case_tools
from services.tools.context import ToolContext
from services.tools.tests.test_tools import (  # noqa: F401 - pytest fixtures
    CASE,
    ENV,
    SEA_ETA,
    T0,
    carrier,
    ctx,
    photo,
    reference_options,
)


@pytest.fixture
def api(ctx: ToolContext, s3: Any, bus: RecordingBus, photo: Signal, carrier: Signal) -> Api:  # noqa: F811
    calc.calc_impact(
        ctx,
        CASE,
        recovery_at=SEA_ETA.isoformat(),
        recovery_source_ref=f"signal:{carrier.signal_id}/ETA",
    )
    proposed = case_tools.propose_plan(
        ctx,
        CASE,
        {"options": reference_options(ctx, photo), "chosen": ["C", "A"], "rationale": "C+A"},
    )
    assert proposed["accepted"] is True
    return Api(
        dynamodb=ctx.dynamodb,
        intake=Intake(
            dynamodb=ctx.dynamodb, raw=RawStore(s3, RAW_BUCKET), bus=bus, component="api", env=ENV
        ),
        bus=bus,
        clock=lambda: T0,
        env=ENV,
        sap=ctx.sap,
    )


def call(api: Api, method: str, resource: str, body: Any = None, query: Any = None) -> Any:
    response = api.handle(
        {
            "httpMethod": method,
            "resource": resource,
            "pathParameters": {"id": CASE},
            "queryStringParameters": query,
            "headers": {},
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {
                "authorizer": {"claims": {"sub": "u-1", "cognito:groups": "planner"}}
            },
        }
    )
    return response["statusCode"], json.loads(response["body"])


def case_items(ctx: ToolContext) -> list[dict[str, Any]]:  # noqa: F811
    return sorted(
        ctx.dynamodb.query(
            TableName="aera-test-cases",
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": f"CASE#{CASE}"}},
        )["Items"],
        key=lambda i: i["SK"]["S"],
    )


def test_fr_sim_01_projection_of_the_plan_of_record(api: Api) -> None:
    status, body = call(api, "GET", "/cases/{id}/projection")

    assert status == 200 and body["option"] == "plan"
    assert body["baseline"]["1010"]["stockouts"][0] == "2026-10-05T14:12:00Z"
    assert body["projection"]["1010"]["stockouts"][0] == "2026-10-06T15:00:00Z"  # T0 + 31 h
    assert body["projection"]["1020"]["points"][0]["stock"] == 480


def test_at_19_what_if_shows_a_new_projection_and_checks_and_changes_nothing(
    api: Api,
    ctx: ToolContext,  # noqa: F811
) -> None:
    before = case_items(ctx)

    status, body = call(
        api, "POST", "/cases/{id}/whatif", {"optionId": "C", "params": {"qty": 400}}
    )

    assert status == 200, body
    assert body["option"]["coverageUnits"] == 400
    # 310 - 5 h x 50 + 400 = 460 at T0 + 5 h, gone 9.2 h later.
    stockout = (T0 + timedelta(hours=14, minutes=12)).isoformat().replace("+00:00", "Z")
    assert body["projection"]["1010"]["stockouts"][0] == stockout
    assert {c["checkId"] for c in body["checks"]} == {f"V-{n:02d}" for n in range(1, 13)}
    assert all(c["passed"] for c in body["checks"]), body["checks"]
    assert case_items(ctx) == before  # plan of record, drafts and case untouched


def test_a_what_if_the_tools_refuse_is_a_clear_400(api: Api) -> None:
    status, body = call(
        api, "POST", "/cases/{id}/whatif", {"optionId": "C", "params": {"qty": 700}}
    )

    assert status == 400 and "free above minimum cover" in body["detail"]


def test_a_what_if_without_original_parameters_is_refused(api: Api, ctx: ToolContext) -> None:  # noqa: F811
    draft = next(
        item
        for item in case_items(ctx)
        if item["SK"]["S"].startswith("DRAFT#") and item["actionType"]["S"] == "STO"
    )
    ctx.dynamodb.delete_item(
        TableName="aera-test-cases",
        Key={"PK": draft["PK"], "SK": draft["SK"]},
    )

    status, body = call(
        api, "POST", "/cases/{id}/whatif", {"optionId": "C", "params": {"qty": 400}}
    )

    assert status == 400 and "no recalculation parameters" in body["detail"]
