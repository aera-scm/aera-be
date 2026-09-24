"""Supervisor runs through the real Strands loop (SRD 6.3, 6.18, BR-11, UC-04, UC-05, UC-06).

The model is scripted; tools, hooks, stores and the Mirror are real. What is tested is the
harness: tools only through the catalogue, the planner round trip, termination, limits,
escalation and the trace, not the model's judgement.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from seed_config import rate_card_items

from services.agent.harness import Harness
from services.agent.hooks import Limits
from services.agent.tests.scripted import Messages, ScriptedModel, Turn, last_result
from services.agent.tools import local_tools
from services.case_service.handler import CaseService
from services.conftest import RecordingBus
from services.mrp_poller.handler import MrpPoller
from services.shared.cases import CaseStore
from services.shared.models import (
    CaseStatus,
    ExtractedField,
    FieldStatus,
    Signal,
    SignalChannel,
    SignalStatus,
    new_ulid,
)
from services.shared.runs import RunStore
from services.shared.sap_client import SapClient
from services.shared.signals import SignalStore
from services.shared.trace import TraceStore
from services.tools.context import ToolContext

ENV = "test"
T0 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
CASE = "EXC-2026-0914"


def add_signal(
    dynamodb: Any,
    channel: SignalChannel,
    text: str,
    name: str,
    value: str,
    conf: float,
    status: FieldStatus,
) -> Signal:
    signal_id = new_ulid()
    record = Signal(
        signal_id=signal_id,
        channel=channel,
        sender_id="x",
        sender_verified=True,
        supplier_id="1000234",
        received_at=T0,
        raw_s3_key=f"raw/{signal_id}",
        raw_sha256="0" * 64,
        normalized_text=text,
        po_number="4500001234",
        material="MAT-48219",
        case_id=CASE,
        status=SignalStatus.ACCEPTED,
        fields=[
            ExtractedField(
                field_id=f"{signal_id}-01",
                signal_id=signal_id,
                name=name,  # type: ignore[arg-type]
                value=value,
                confidence=conf,
                status=status,
            )
        ],
    )
    SignalStore(dynamodb, ENV).create(record)
    CaseStore(dynamodb, ENV).add_signal(CASE, signal_id)
    return record


@pytest.fixture
def ctx(dynamodb: Any, sap: SapClient, bus: RecordingBus) -> ToolContext:
    for item in rate_card_items("2026-09-24T00:00:00Z"):
        dynamodb.put_item(TableName="aera-test-config", Item=item)
    CaseStore(dynamodb, ENV).seed_counter(2026, 913)
    MrpPoller(sap=sap, bus=bus, env=ENV).poll()
    CaseService(dynamodb=dynamodb, sap=sap, bus=bus, clock=lambda: T0, env=ENV).on_mrp(
        bus.details("MrpExceptionsPolled")[0]["data"]
    )
    add_signal(
        dynamodb,
        SignalChannel.WHATSAPP,
        "only this much ready",
        "QUANTITY",
        "640",
        0.71,
        FieldStatus.UNCONFIRMED,
    )
    add_signal(
        dynamodb,
        SignalChannel.CARRIER,
        "Carrier status DELAYED",
        "ETA",
        (T0 + timedelta(days=9)).isoformat(),
        1.0,
        FieldStatus.CONFIRMED,
    )
    return ToolContext(sap=sap, dynamodb=dynamodb, bus=bus, clock=lambda: T0, env=ENV)


def start(ctx: ToolContext, run_id: str, status: CaseStatus = CaseStatus.TRIAGED) -> dict[str, Any]:
    """What run-starter does before invoking the runtime."""
    assert RunStore(ctx.dynamodb, ENV).claim(CASE, run_id)
    ctx.cases.transition(CASE, CaseStatus.INVESTIGATING, actor="system", expected=status)
    return {"caseId": CASE, "runId": run_id, "mode": "investigate", "reason": "test"}


def harness(ctx: ToolContext, turns: list[Turn], **limits: Any) -> tuple[Harness, ScriptedModel]:
    model = ScriptedModel(turns)
    return Harness(ctx=ctx, model=model, tools=local_tools, limits=Limits(**limits)), model


def field_of(messages: Messages, name: str) -> dict[str, Any]:
    evidence = last_result(messages, "get_case_evidence")
    return next(f for f in evidence["fields"] if f["name"] == name)


def test_uc_05_unconfirmed_quantity_ends_the_run_with_a_planner_question(
    ctx: ToolContext, bus: RecordingBus
) -> None:
    run, model = harness(
        ctx,
        [
            Turn("I read the evidence first.", "get_case_evidence", {"caseId": CASE}),
            Turn(
                "The photographed quantity is unconfirmed, so I ask the planner.",
                "ask_planner",
                lambda m: {
                    "caseId": CASE,
                    "question": "Is 640 the quantity ready to ship?",
                    "fieldId": field_of(m, "QUANTITY")["fieldId"],
                },
            ),
            Turn("This must never be reached.", "sap_get_stock", {}),
        ],
    )

    outcome = run.run(start(ctx, "run-1"))

    assert outcome.end_reason == "WAITING_PLANNER"
    assert model.calls == 2  # the loop stopped right after the terminal tool
    case = ctx.cases.get(CASE)
    assert case is not None and case.status is CaseStatus.WAITING_PLANNER
    assert case.active_run_id is None
    ended = bus.details("RunEnded")[-1]["data"]
    assert (ended["endReason"], ended["promptVersion"]) == ("WAITING_PLANNER", "supervisor_v1")
    kinds = [e.kind for e in TraceStore(ctx.dynamodb, ENV).events(CASE)]
    assert kinds == [
        "SYSTEM",
        "AGENT",
        "TOOL_CALL",
        "TOOL_RESULT",
        "AGENT",
        "TOOL_CALL",
        "TOOL_RESULT",
        "SYSTEM",
    ]


def confirm_quantity(ctx: ToolContext) -> None:
    for signal in ctx.signals.for_case(CASE):
        if signal.fields and signal.fields[0].name == "QUANTITY":
            field = signal.fields[0].model_copy(
                update={"status": FieldStatus.CONFIRMED, "confirmed_by": "user:planner-1"}
            )
            ctx.signals.save(signal.model_copy(update={"fields": [field]}))


def plan_turns() -> list[Turn]:
    def option(oid: str, name: str, tool_result: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": oid,
            "name": name,
            "actions": tool_result["actions"],
            "coverageUnits": tool_result["coverageUnits"],
            "arrival": tool_result["arrival"],
            "costUsd": tool_result["costUsd"],
            "costSourceRef": tool_result["costSourceRef"],
            "figures": tool_result["figures"],
            "rationale": name,
        }

    def results_of(messages: Messages) -> list[dict[str, Any]]:
        found = []
        for message in messages:
            for block in message.get("content", []):
                for content in (block.get("toolResult") or {}).get("content", []):
                    if "json" in content and "costSourceRef" in content["json"]:
                        found.append(content["json"])
        return found

    def plan(messages: Messages) -> dict[str, Any]:
        air, alternate, sto = results_of(messages)
        return {
            "caseId": CASE,
            "plan": {
                "options": [
                    option("A", "Air freight the ready 640", air),
                    option("B", "Alternate supplier Halim Presisi", alternate),
                    option("C", "Transfer 600 from plant 1020", sto),
                ],
                "chosen": ["C", "A"],
                "rationale": "STO arrives before stock-out; air freight covers the rest.",
            },
        }

    def eta(messages: Messages) -> dict[str, Any]:
        return field_of(messages, "ETA")

    return [
        Turn("I read the evidence again.", "get_case_evidence", {"caseId": CASE}),
        Turn("I check the purchase order.", "sap_get_purchase_order", {"poNumber": "4500001234"}),
        Turn(
            "I check stock at the plant.",
            "sap_get_stock",
            {"material": "MAT-48219", "plant": "1010"},
        ),
        Turn(
            "I quantify the impact until the sea delivery.",
            "calc_impact",
            lambda m: {
                "caseId": CASE,
                "recoveryAt": eta(m)["value"],
                "recoverySourceRef": eta(m)["sourceRef"],
            },
        ),
        Turn(
            "I look for other sources.",
            "find_sources",
            {
                "material": "MAT-48219",
                "plant": "1010",
                "qtyNeeded": 1240,
                "needBy": "2026-10-05T14:12:00Z",
            },
        ),
        Turn(
            "I price the air freight of the confirmed partial.",
            "calc_option",
            lambda m: {
                "caseId": CASE,
                "actionType": "AIR_FREIGHT",
                "params": {"qtyFieldId": field_of(m, "QUANTITY")["fieldId"]},
            },
        ),
        Turn(
            "I price the alternate supplier.",
            "calc_option",
            {
                "caseId": CASE,
                "actionType": "ALTERNATE_SUPPLIER",
                "params": {"supplierId": "1000871", "qty": 800},
            },
        ),
        Turn(
            "I price the transfer from 1020.",
            "calc_option",
            {"caseId": CASE, "actionType": "STO", "params": {"fromPlant": "1020", "qty": 600}},
        ),
        Turn("I propose C plus A.", "propose_plan", plan),
    ]


def test_uc_04_uc_06_planner_answer_starts_a_run_that_proposes_the_reference_plan(
    ctx: ToolContext, bus: RecordingBus
) -> None:
    first, _ = harness(
        ctx,
        [
            Turn("Evidence.", "get_case_evidence", {"caseId": CASE}),
            Turn(
                "Ask.",
                "ask_planner",
                lambda m: {
                    "caseId": CASE,
                    "question": "640?",
                    "fieldId": field_of(m, "QUANTITY")["fieldId"],
                },
            ),
        ],
    )
    first.run(start(ctx, "run-1"))
    confirm_quantity(ctx)

    second, model = harness(ctx, plan_turns())
    outcome = second.run(start(ctx, "run-2", status=CaseStatus.WAITING_PLANNER))

    assert outcome.end_reason == "PLAN"
    case = ctx.cases.get(CASE)
    assert case is not None and case.status is CaseStatus.PLAN_PROPOSED and case.plan_version == 1
    [proposed] = bus.details("PlanProposed")
    assert proposed["data"] == {"caseId": CASE, "planVersion": 1}
    plan_result = last_result(model.seen[-1] + [], "calc_option")
    assert plan_result["costSourceRef"] == "ratecard:RC-STO-1020-1010"
    # The second run was given the first run's summary (SRD 6.18 case context).
    opening = model.seen[0][0]["content"][0]["text"]
    assert "run-1" in opening and "WAITING_PLANNER" in opening
    history = RunStore(ctx.dynamodb, ENV).history(CASE)
    assert [r["endReason"] for r in history] == ["WAITING_PLANNER", "PLAN"]


def test_br_11_iteration_limit_escalates_by_the_harness(ctx: ToolContext) -> None:
    run, model = harness(
        ctx,
        [Turn("Again.", "sap_get_stock", {"material": "MAT-48219", "plant": "1010"})],
        max_iterations=3,
    )

    outcome = run.run(start(ctx, "run-1"))

    assert outcome.end_reason == "LIMIT_ITERATIONS"
    assert model.calls == 3
    case = ctx.cases.get(CASE)
    assert case is not None and (case.status, case.tier) == (CaseStatus.ESCALATED, 3)


def test_br_11_token_limit(ctx: ToolContext) -> None:
    run, _ = harness(
        ctx,
        [Turn("Big.", "sap_get_stock", {"material": "MAT-48219", "plant": "1010"}, tokens=90_000)],
        max_tokens=150_000,
    )
    assert run.run(start(ctx, "run-1")).end_reason == "LIMIT_TOKENS"


def test_br_11_wall_clock_limit(ctx: ToolContext) -> None:
    ticks = iter(range(0, 1000, 200))
    run, _ = harness(
        ctx,
        [Turn("Slow.", "sap_get_stock", {"material": "MAT-48219", "plant": "1010"})],
        max_seconds=300,
    )
    run.monotonic = lambda: float(next(ticks))
    assert run.run(start(ctx, "run-1")).end_reason == "LIMIT_TIME"


def test_a_run_that_just_stops_is_escalated_not_left_hanging(ctx: ToolContext) -> None:
    run, _ = harness(ctx, [Turn("I think we are fine.")])

    outcome = run.run(start(ctx, "run-1"))

    assert outcome.end_reason == "ESCALATE"
    case = ctx.cases.get(CASE)
    assert case is not None and case.status is CaseStatus.ESCALATED


def test_rejected_plan_goes_back_to_the_model(ctx: ToolContext) -> None:
    run, model = harness(
        ctx,
        [
            Turn("Bad plan.", "propose_plan", {"caseId": CASE, "plan": {"options": []}}),
            Turn("Give up.", "escalate", {"caseId": CASE, "reason": "no plan"}),
        ],
    )

    assert run.run(start(ctx, "run-1")).end_reason == "ESCALATE"
    assert "two or three options" in str(model.seen[-1])


class Broken(ScriptedModel):
    async def stream(  # type: ignore[override]
        self, messages: Messages, *args: Any, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        raise RuntimeError("model unavailable")
        yield {}


def test_two_consecutive_failures_escalate(ctx: ToolContext, bus: RecordingBus) -> None:
    broken = Harness(ctx=ctx, model=Broken([]), tools=local_tools)

    assert broken.run(start(ctx, "run-1")).end_reason == "LIMIT_ERROR"
    case = ctx.cases.get(CASE)
    assert case is not None and case.status is CaseStatus.INVESTIGATING
    assert RunStore(ctx.dynamodb, ENV).claim(CASE, "run-2")
    assert broken.run({"caseId": CASE, "runId": "run-2"}).end_reason == "LIMIT_ERROR"
    case = ctx.cases.get(CASE)
    assert case is not None and case.status is CaseStatus.ESCALATED
