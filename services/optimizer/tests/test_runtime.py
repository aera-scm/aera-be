"""BR-20 at runtime: two cases compete for the same 600 units at plant 1020 (AT-18, V-15)."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from services.optimizer.handler import lambda_handler
from services.optimizer.runtime import (
    PortfolioService,
    allocation_for,
    conforms,
    donor_allocation,
    resized_plan,
)
from services.shared.cases import CaseStore
from services.shared.dynamo import to_item
from services.shared.models import Case, CaseStatus, PlanRecord, ProposedPlan, Signal
from services.tools import calc, case_tools
from services.tools.context import ToolContext
from services.tools.tests.test_tools import (  # noqa: F401 - pytest fixtures
    CASE,
    ENV,
    T0,
    confirm,
    ctx,
    photo,
    reference_options,
)
from services.verifier.logic import _portfolio_check

OTHER = "EXC-2026-0950"
DONOR = "DONOR#MAT-48219#1020"


def solver(event: dict[str, Any]) -> dict[str, Any]:
    return lambda_handler(event, None)


def config(context: ToolContext, item: dict[str, Any]) -> None:
    context.dynamodb.put_item(TableName=f"aera-{ENV}-config", Item=to_item(item))


@pytest.fixture
def competing(ctx: ToolContext, photo: Signal) -> ToolContext:  # noqa: F811
    """The reference case (C transfer + A air) and a 1030 case that can only use the transfer.

    1030's customer has priority 3 (`PRIO#`), so its lost revenue weighs more; air freight
    capacity comes from a `FREIGHT#` entry and the partial from the confirmed photo quantity.
    """
    config(
        ctx,
        {
            "PK": "RATE#RC-STO-1020-1030",
            "entryId": "RC-STO-1020-1030",
            "actionType": "STO",
            "fromPlant": "1020",
            "toPlant": "1030",
            "unitCostUsd": Decimal("0"),
            "fixedCostUsd": Decimal("3900"),
            "leadTimeHours": Decimal("6"),
            "validFrom": "2026-01-01",
            "validTo": "9999-12-31",
        },
    )
    config(
        ctx,
        {
            "PK": "FREIGHT#1000234#1010",
            "supplierId": "1000234",
            "plant": "1010",
            "qtyPerDay": Decimal("2000"),
            "validFrom": "2026-01-01",
            "validTo": "9999-12-31",
        },
    )
    # Customer 3000306 (sales order 2000510) is a priority-3 customer (ADR-0021).
    config(ctx, {"PK": "PRIO#3000306", "customerId": "3000306", "weight": Decimal("3")})
    calc.calc_impact(ctx, CASE)
    plan = {
        "options": reference_options(ctx, photo),
        "chosen": ["C", "A"],
        "rationale": "Transfer now, air freight behind it.",
    }
    assert case_tools.propose_plan(ctx, CASE, plan)["accepted"]

    store = CaseStore(ctx.dynamodb, ENV, clock=lambda: T0)
    store.create(
        Case(
            case_id=OTHER,
            type="MRP_EXCEPTION",
            material="MAT-48219",
            plant="1030",
            status=CaseStatus.PLAN_PROPOSED,
            priority_score=Decimal("900000"),
            rar_usd=Decimal("900000"),
            created_at=T0,
            updated_at=T0,
        ),
        actor="system",
    )
    ctx.dynamodb.put_item(
        TableName=f"aera-{ENV}-cases",
        Item=to_item(
            {
                "PK": f"CASE#{OTHER}",
                "SK": "IMPACT",
                "unitsAtRisk": Decimal("600"),
                "rarUsd": Decimal("900000"),
                "productionOrdersAtRisk": [
                    {
                        "productionOrder": "1009001",
                        "requiredAt": (T0 + timedelta(hours=12)).isoformat(),
                        "unitsShort": Decimal("600"),
                        "sourceRef": "SAP:API_PRODUCTION_ORDER_2_SRV/A_ProductionOrderComponent_2",
                    }
                ],
                "salesOrdersAtRisk": [
                    {
                        "salesOrder": "2000510",
                        "item": "10",
                        "netUsd": Decimal("900000"),
                        "sourceRef": "SAP:API_SALES_ORDER_SRV/A_SalesOrderItem",
                    }
                ],
            }
        ),
    )
    option = {
        "id": "C",
        "name": "Transfer from 1020",
        "actions": [
            {
                "type": "CREATE_STO",
                "fromPlant": "1020",
                "toPlant": "1030",
                "material": "MAT-48219",
                "qty": 600,
                "deliveryDate": (T0 + timedelta(hours=6)).date().isoformat(),
            }
        ],
        "coverageUnits": 600,
        "arrival": (T0 + timedelta(hours=6)).isoformat(),
        "costUsd": 3900,
        "costSourceRef": "ratecard:RC-STO-1020-1030",
        "rationale": "Option C transfers 600 PC from plant 1020.",
    }
    record = PlanRecord(
        plan=ProposedPlan.model_validate(
            {
                "caseId": OTHER,
                "planVersion": 1,
                "options": [option],
                "chosen": ["C"],
                "totalCostUsd": 3900,
                "coverageUnits": 600,
                "rationale": "Transfer.",
            }
        ),
        proposed_at=T0 + timedelta(minutes=5),
    )
    ctx.dynamodb.put_item(
        TableName=f"aera-{ENV}-cases",
        Item=to_item(
            {"PK": f"CASE#{OTHER}", "SK": "PLAN#1", **record.model_dump(mode="json", by_alias=True)}
        ),
    )
    ctx.dynamodb.update_item(
        TableName=f"aera-{ENV}-cases",
        Key={"PK": {"S": f"CASE#{OTHER}"}, "SK": {"S": "META"}},
        UpdateExpression="SET planVersion = :v",
        ExpressionAttributeValues={":v": {"N": "1"}},
    )
    return ctx


def test_br_20_at_18_two_cases_share_the_1020_donor_jointly(competing: ToolContext) -> None:
    service = PortfolioService(competing, solver, Decimal("2"))

    record = service.solve_for(CASE)

    assert record is not None and record["caseIds"] == [CASE, OTHER]
    assert record["solverStatus"] in ("OPTIMAL", "FEASIBLE")
    donor = next(c for c in record["capacities"] if c["resource"] == DONOR)
    given = sum(
        (donor_allocation(record, case) or {}).get(DONOR, Decimal(0)) for case in (CASE, OTHER)
    )
    assert given <= donor["quantity"]  # never double-allocated (BR-08)
    assert record["objective"] <= record["singleObjective"]
    assert record["savingVsSingle"] == record["singleObjective"] - record["objective"]
    assert all(c["sourceRef"] for c in record["capacities"])  # sourced, never invented
    stored = competing.dynamodb.get_item(
        TableName=f"aera-{ENV}-cases",
        Key={"PK": {"S": f"CASE#{OTHER}"}, "SK": {"S": "PORTFOLIO"}},
    )
    assert "Item" in stored


def test_br_20_priority_weighting_moves_the_donor_to_the_priority_customer(
    competing: ToolContext,
) -> None:
    record = PortfolioService(competing, solver, Decimal("2")).solve_for(CASE)
    assert record is not None
    other = allocation_for(record, OTHER)
    # The reference case still has air freight; the 1030 case has nothing but the transfer.
    assert other and other["C"] > 0
    assert record["savingVsSingle"] > 0


def test_adr_0021_air_freight_without_freight_capacity_is_left_out(
    competing: ToolContext,
) -> None:
    competing.dynamodb.delete_item(
        TableName=f"aera-{ENV}-config", Key={"PK": {"S": "FREIGHT#1000234#1010"}}
    )
    record = PortfolioService(competing, solver, Decimal("2")).solve_for(CASE)
    assert record is not None
    reason = "no sourced capacity or rate (ADR-0021)"
    assert {"caseId": CASE, "optionId": "A", "reason": reason} in record["excluded"]
    assert not any(c["id"] == f"{CASE}/A" for c in record["candidateActions"])


def test_fr_opz_01_a_case_without_competitor_has_no_portfolio(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
) -> None:
    calc.calc_impact(ctx, CASE)
    plan = {"options": reference_options(ctx, photo), "chosen": ["C", "A"], "rationale": "x"}
    assert case_tools.propose_plan(ctx, CASE, plan)["accepted"]
    assert PortfolioService(ctx, solver, Decimal("2")).solve_for(CASE) is None


def test_fr_opz_03_resized_transfer_is_repriced_through_calc_option(
    competing: ToolContext,
) -> None:
    case = competing.cases.get(CASE)
    assert case is not None
    item = competing.dynamodb.get_item(
        TableName=f"aera-{ENV}-cases",
        Key={"PK": {"S": f"CASE#{CASE}"}, "SK": {"S": "PLAN#1"}},
    )["Item"]
    from services.shared.dynamo import from_item

    record = PlanRecord.from_stored(from_item(item))
    assert not conforms(record, {"C": Decimal(200), "A": Decimal(640)})

    revised = resized_plan(competing, case, record, {"C": Decimal(200), "A": Decimal(640)})

    assert revised is not None and revised.plan_version == 2
    c = next(o for o in revised.options if o.id == "C")
    sto = c.actions[0]
    assert c.coverage_units == 200 and sto.type == "CREATE_STO" and sto.qty == 200
    assert revised.coverage_units == 840 and sorted(revised.chosen) == ["A", "C"]
    assert resized_plan(competing, case, record, {}) is None


def test_v_15_plan_must_match_the_latest_portfolio_allocation(
    competing: ToolContext,
) -> None:
    item = competing.dynamodb.get_item(
        TableName=f"aera-{ENV}-cases",
        Key={"PK": {"S": f"CASE#{CASE}"}, "SK": {"S": "PLAN#1"}},
    )["Item"]
    from services.shared.dynamo import from_item

    plan = PlanRecord.from_stored(from_item(item)).plan
    selected = [o for o in plan.options if o.id in plan.chosen]

    assert _portfolio_check(plan, selected, None).passed  # no competing case
    assert _portfolio_check(plan, selected, {"C": Decimal(600), "A": Decimal(640)}).passed
    over = _portfolio_check(plan, selected, {"C": Decimal(200), "A": Decimal(640)})
    assert not over.passed and over.blocking and "allocated 200" in over.detail
    assert not _portfolio_check(plan, selected, {}).passed


def test_br_08_an_auto_approved_transfer_is_not_offered_again(competing: ToolContext) -> None:
    """The reference transfer already ran as Tier 1 (BR-22): its 600 PC are held in the
    ledger, so the portfolio must not count that option as a free candidate a second time."""
    competing.dynamodb.put_item(
        TableName=f"aera-{ENV}-cases",
        Item=to_item(
            {
                "PK": f"CASE#{CASE}",
                "SK": "ROUTE#1",
                "tier": 2,
                "parts": [{"id": "p1", "options": ["C"], "tier": 1}],
            }
        ),
    )
    record = PortfolioService(competing, solver, Decimal("2")).solve_for(OTHER)
    assert record is not None
    assert not any(c["id"] == f"{CASE}/C" for c in record["candidateActions"])
    assert any(e["optionId"] == "C" and e["caseId"] == CASE for e in record["excluded"])


def test_fr_cht_01_planner_constraints_bind_the_portfolio(competing: ToolContext) -> None:
    """Live 2026-10-04 (AT-10): under "under USD 30,000" the solver added the USD 38,200 air
    option back into the reference plan. An option outside the constraints is never offered."""
    competing.dynamodb.put_item(
        TableName=f"aera-{ENV}-cases",
        Item=to_item({"PK": f"CASE#{CASE}", "SK": "CONSTRAINTS", "maxCostUsd": 30000}),
    )
    record = PortfolioService(competing, solver, Decimal("2")).solve_for(CASE)
    assert record is not None
    assert not any(c["id"] == f"{CASE}/A" for c in record["candidateActions"])
    assert any(
        e["optionId"] == "A" and "constraints" in e["reason"] and e["caseId"] == CASE
        for e in record["excluded"]
    )


def test_br_07_br_08_donor_capacity_leaves_cover_after_held_reservations(
    competing: ToolContext,
) -> None:
    """Live 2026-10-05 (AT-09): with 600 PC already held for an executed transfer, the donor
    may give only what is left above its minimum cover. Held stock and the cover both leave
    the donor; taking the smaller of the two let a second transfer through to the ledger."""
    from services.execution.ledger import Ledger

    service = PortfolioService(competing, solver, Decimal("2"))
    before = service.donor_capacity("MAT-48219", "1020")["quantity"]
    Ledger(competing.dynamodb, ENV).reserve(
        material="MAT-48219",
        plant="1020",
        reservation_id="earlier-part",
        case_id="EXC-2026-0999",
        quantity=Decimal(100),
        available=Decimal(100000),
        source_ref="SAP:stock",
    )

    assert service.donor_capacity("MAT-48219", "1020")["quantity"] == before - 100
