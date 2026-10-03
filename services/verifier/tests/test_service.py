"""Verifier and routing at runtime on the reference scenario, against a real Mirror
(FR-VER-01..03, FR-RTE-01..08, BR-05, BR-18, BR-22, BR-23, AT-05, AT-11, AT-16, AT-29).

The agent's part is played by the real tools: options come from `calc_option` and the plan
from `propose_plan`. Only the Guardrail grounding score is supplied by the test.
"""

import itertools
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
from services.verifier.automated_reasoning import PolicyAssessment
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
    plan = ControlStore(ctx.dynamodb, ENV).get(CASE, "PLAN#1") or {}
    assert plan["projection"]["baseline"]["1010"]["points"]
    assert plan["projection"]["projection"]["1020"]["points"]
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


def test_at_24_policy_disagreement_escalates_before_any_execution(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
    verifier: VerifierService,
) -> None:
    propose(ctx, photo, ["C"])
    verifier.reasoning = lambda verified, proposed, policy: PolicyAssessment(
        "INVALID", "Synthetic policy facts", ("INVALID",), "policy-arn"
    )

    result = verifier.handle(CASE, 1)

    assert result["tier"] == 3
    assert result["reason"] == "AUTOMATED_REASONING_DISAGREEMENT"
    assert outbox(ctx) == ["PlanRouted"]
    case = ctx.cases.get(CASE)
    assert case is not None and case.status is CaseStatus.ESCALATED
    plan = ControlStore(ctx.dynamodb, ENV).get(CASE, "PLAN#1") or {}
    assert plan["automatedReasoning"]["status"] == "INVALID"
    assert plan["automatedReasoning"]["policyArn"] == "policy-arn"


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


def test_fr_ver_01_reference_plan_verifies_on_a_running_clock(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
    verifier: VerifierService,
) -> None:
    """A deployed clock moves between reads: the agent prices options over several
    milliseconds and the Verifier runs minutes later. Neither may fail V-01, V-04, V-07 or
    V-10 on a plan whose sources did not change."""
    ticks = itertools.count()
    agent = replace(ctx, clock=lambda: T0 + timedelta(milliseconds=next(ticks)))
    propose(agent, photo, ["C", "A"])
    later = replace(verifier, clock=lambda: T0 + timedelta(minutes=2, milliseconds=next(ticks)))

    result = later.handle(CASE, 1)

    assert failed(ctx) == {"V-06/B"}
    assert result["tier"] == 2


def test_fr_imp_03_options_without_figures_get_the_sourced_draft_figures(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
    verifier: VerifierService,
) -> None:
    """The live agent sent options with empty `figures` (sources only in the rationale):
    the plan must carry the draft's sourced figures, and V-01 must pass on them."""
    options = [{**option, "figures": []} for option in reference_options(ctx, photo)]
    result = case_tools.propose_plan(
        ctx, CASE, {"options": options, "chosen": ["C", "A"], "rationale": "Options C+A"}
    )
    assert result["accepted"] is True, result

    verified = verifier.handle(CASE, 1)

    plan = ControlStore(ctx.dynamodb, ENV).get(CASE, "PLAN#1") or {}
    names = {o["id"]: {f["name"] for f in o["figures"]} for o in plan["plan"]["options"]}
    assert names["C"] == {"donorFree", "costUsd", "arrival"}
    assert {"airQuantity", "costUsd", "arrival"} <= names["A"]
    assert failed(ctx) == {"V-06/B"}
    assert verified["tier"] == 2


def test_fr_ver_01_agent_written_times_without_fractions_still_verify(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
    verifier: VerifierService,
) -> None:
    """Live: the model copied arrivals as `...T08:15:15Z` while the draft held
    `08:15:15.290185+00:00`, and rewrote time figures in ISO `Z` form. Prices are taken at
    whole seconds and time figures compare as instants, so neither fails V-01/V-04/V-10."""
    ticks = itertools.count()
    agent = replace(ctx, clock=lambda: T0 + timedelta(seconds=next(ticks), microseconds=290185))

    def iso(value: Any) -> str:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    options = []
    for option in reference_options(agent, photo):
        actions = [
            {**a, "arrival": iso(a["arrival"])} if "arrival" in a else a for a in option["actions"]
        ]
        figures = [
            {**f, "value": iso(f["value"])} if f["name"] in ("arrival", "remainderAt") else f
            for f in option["figures"]
        ]
        options.append(
            {**option, "arrival": iso(option["arrival"]), "actions": actions, "figures": figures}
        )
    result = case_tools.propose_plan(
        agent, CASE, {"options": options, "chosen": ["C", "A"], "rationale": "Options C+A"}
    )
    assert result["accepted"] is True, result
    later = replace(verifier, clock=lambda: T0 + timedelta(minutes=2, seconds=next(ticks)))

    verified = later.handle(CASE, 1)

    assert failed(ctx) == {"V-06/B"}
    assert verified["tier"] == 2


def test_fr_ver_03_grounding_source_is_sentences_from_trusted_reads(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
    verifier: VerifierService,
) -> None:
    """ADR-0032: the rationale is checked against the Verifier's own reads as sentences
    (purchase order, impact, options, chosen totals, projection, suppliers), never against
    the agent's words."""
    seen: list[str] = []

    def grounding(rationale: str, source: str, query: str) -> Grounding:
        seen.append(source)
        return GOOD

    propose(ctx, photo, ["C", "A"], "AGENT-ONLY-PHRASE transfer now, air freight behind it")
    replace(verifier, grounding=grounding).handle(CASE, 1)

    [source] = seen
    assert "AGENT-ONLY-PHRASE" not in source
    assert "Purchase order 4500001234 item 10" in source and "PC ordered." in source
    assert "Option C (CREATE_STO): 600 PC, cost USD 4,100" in source
    assert "The chosen options C + A together cost USD 42,300 and cover 1,240 PC." in source
    assert "With the chosen options, plant 1010" in source
    assert "Supplier 1000871" in source and "compliance status" in source


def test_br_23_reference_approval_window_is_open_with_the_sto_in_place(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
    verifier: VerifierService,
) -> None:
    """ADR-0034: with the 5 h STO bridging the stock-out, the 17 h air freight part gets a
    deadline after routing (previously stock-out minus 17 h, already past)."""
    propose(ctx, photo, ["C", "A"])

    verifier.handle(CASE, 1)

    route = ControlStore(ctx.dynamodb, ENV).get(CASE, "ROUTE#1") or {}
    [pending] = [p for p in route["parts"] if p["tier"] == 2]
    deadline = datetime.fromisoformat(str(pending["deadline"]))
    assert T0 < deadline <= T0 + timedelta(hours=4)
