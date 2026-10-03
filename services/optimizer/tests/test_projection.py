"""FR-SIM-01/02: projections match hand-calculated values (WP-9 definition of done).

Reference: 310 PC at 1010, 50 PC/h, orders due at T0, +8 h, +16 h, +24 h and +9 d 4 h; the
delayed order (1,600 PC) arrives with the sea shipment at T0 + 9 d. Donor 1020: 1,080 PC at
10 PC/h. Transfer (C): 600 PC in 5 h. Air freight (A): 640 PC in 17 h.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

from services.optimizer.inputs import CaseProjector
from services.optimizer.projection import Movement, PlantInputs, Requirement, project
from services.shared.models import Option, Signal
from services.tools import calc
from services.tools.context import ToolContext
from services.tools.tests.test_tools import (  # noqa: F401 - pytest fixtures
    CASE,
    SEA_ETA,
    T0,
    carrier,
    ctx,
    photo,
    reference_options,
)

D = Decimal
H = timedelta(hours=1)


def reference_inputs() -> PlantInputs:
    orders = [("1000100", 0, 400), ("1000101", 8, 400), ("1000102", 16, 400)]
    orders += [("1000103", 24, 350), ("1000104", 220, 400)]
    return PlantInputs(
        plant="1010",
        on_hand=D(310),
        rate=D(50),
        movements=(Movement(SEA_ETA, D(1600), "PO 4500001234", "SAP:x"),),
        requirements=tuple(Requirement(o, T0 + h * H, D(q), "SAP:y") for o, h, q in orders),
    )


def test_fr_sim_01_02_baseline_by_hand() -> None:
    result = project(reference_inputs(), T0)

    assert result.stock_at(T0) == 310
    assert result.stock_at(T0 + 6 * H) == 10
    assert result.stock_at(T0 + 7 * H) == 0
    assert result.stock_at(T0 + timedelta(days=9)) == 1600  # the sea shipment lands
    assert result.stock_at(T0 + timedelta(days=10)) == 400  # 1,600 - 24 h x 50
    assert result.first_stockout == T0 + timedelta(hours=6, minutes=12)
    first, second = result.windows
    assert (first.start, first.end) == (T0 + timedelta(hours=6, minutes=12), SEA_ETA)
    assert first.orders == ("1000101", "1000102", "1000103")
    assert first.units_short == D(50) * D("209.8")
    assert (second.start, second.end) == (SEA_ETA + 32 * H, None)  # 1,600 / 50 = 32 h
    assert len(result.points) == 73 + 27  # hourly to 72 h, then daily days 4 to 30


def test_fr_sim_01_transfer_and_air_freight_by_hand() -> None:
    base = reference_inputs()
    sto = Movement(T0 + 5 * H, D(600), "STO", "ratecard:x")
    air = Movement(T0 + 17 * H, D(640), "Air", "ratecard:y")

    with_c = project(base.plus(sto), T0)
    with_ca = project(base.plus(sto, air), T0)

    assert with_c.stock_at(T0 + 5 * H) == 660  # 310 - 250 + 600
    assert with_c.first_stockout == T0 + timedelta(hours=18, minutes=12)  # + 660 / 50 h
    assert with_ca.stock_at(T0 + 17 * H) == 700  # 660 - 600 + 640
    assert with_ca.first_stockout == T0 + 31 * H


def test_a_plant_without_stock_is_stopped_from_the_start() -> None:
    empty = PlantInputs("1010", D(0), D(50))

    result = project(empty, T0)

    assert result.first_stockout == T0
    [window] = result.windows
    assert window.end is None and window.units_short == D(50) * 24 * 30


def test_fr_sim_01_from_sap_for_the_reference_case(
    ctx: ToolContext,  # noqa: F811
    photo: Signal,  # noqa: F811
    carrier: Signal,  # noqa: F811
) -> None:
    calc.calc_impact(
        ctx,
        CASE,
        recovery_at=SEA_ETA.isoformat(),
        recovery_source_ref=f"signal:{carrier.signal_id}/ETA",
    )
    options = {o["id"]: Option.model_validate(o) for o in reference_options(ctx, photo)}
    case = ctx.cases.get(CASE)
    assert case is not None
    projector = CaseProjector(ctx, case)

    baseline = projector.projections([])["1010"]
    plan = projector.projections([options["C"], options["A"]])

    assert baseline.first_stockout == T0 + timedelta(hours=6, minutes=12)
    assert baseline.windows[0].orders == ("1000101", "1000102", "1000103")
    assert plan["1010"].first_stockout == T0 + 31 * H
    # A flies 640 of the 1,600 on order; 960 still come with the sea shipment.
    assert plan["1010"].stock_at(SEA_ETA) == 960
    donor = plan["1020"]
    assert donor.stock_at(T0) == 480  # 1,080 - 600 transferred
    assert donor.first_stockout == T0 + 48 * H  # exactly the two days of cover (BR-07)
    assert any(ref.startswith("SAP:") for ref in baseline.source_refs)
    assert any("RequiredQuantity" in ref for ref in baseline.source_refs)


def _json_is_serialisable(value: Any) -> None:
    import json

    from services.tools.context import jsonable

    json.dumps(jsonable(value))


def test_projection_json_is_plain(ctx: ToolContext) -> None:  # noqa: F811
    _json_is_serialisable(project(reference_inputs(), T0).json())


def test_simulate_plan_tool_compares_options(ctx: ToolContext, carrier: Signal) -> None:  # noqa: F811
    from services.tools.registry import BY_NAME

    calc.calc_impact(
        ctx,
        CASE,
        recovery_at=SEA_ETA.isoformat(),
        recovery_source_ref=f"signal:{carrier.signal_id}/ETA",
    )
    result = BY_NAME["simulate_plan"].invoke(
        ctx,
        {
            "caseId": CASE,
            "options": [
                {"id": "C", "actionType": "STO", "params": {"fromPlant": "1020", "qty": 600}}
            ],
        },
    )

    assert result["baseline"]["1010"]["firstStockout"] == "2026-10-05T14:12:00Z"
    assert result["options"]["C"]["1010"]["firstStockout"] == "2026-10-06T02:12:00Z"
    assert result["combined"]["1020"]["firstStockout"] == "2026-10-07T08:00:00Z"


def test_fr_sim_04_v07_needs_the_donor_projection_to_outlast_its_cover() -> None:
    from services.verifier.logic import Donor, _donor_ok

    cover_end = T0 + timedelta(days=2)
    enough = Donor(D(1080), D(10), D(0), D(1080), T0 + 48 * H, cover_end)
    short = Donor(D(1080), D(10), D(0), D(1080), T0 + 47 * H, cover_end)

    assert _donor_ok(enough, D(600), D(2)) is True
    assert _donor_ok(short, D(600), D(2)) is False
