"""Triage figures from the Mirror's reference scenario (FR-TRI-01, BR-13, FR-IMP-03)."""

from datetime import UTC, datetime
from decimal import Decimal

from services.shared.sap_client import SapClient
from services.shared.triage import assess

T0 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)


def test_reference_case_runs_out_in_6_2_hours_with_usd_4_72m_at_risk(sap: SapClient) -> None:
    triage = assess(sap, "MAT-48219", "1010", T0)

    assert (triage.on_hand, triage.per_hour, triage.hours_to_stockout) == (
        Decimal(310),
        Decimal(50),
        Decimal("6.2"),
    )
    assert triage.stockout_at == datetime(2026, 10, 5, 14, 12, tzinfo=UTC)
    assert triage.rar_usd == Decimal(4_720_000)
    assert triage.priority_score == Decimal(14_160_000)
    assert sorted(e.sales_order for e in triage.exposures) == [
        str(n) for n in range(2000451, 2000461)
    ]


def test_fr_imp_03_every_figure_names_its_sap_source(sap: SapClient) -> None:
    triage = assess(sap, "MAT-48219", "1010", T0)

    by_name = {f.name: f for f in triage.figures}
    assert by_name["onHand"].source_ref.startswith("SAP:API_MATERIAL_STOCK_SRV/A_MatlStkInAcctMod(")
    assert by_name["consumptionPerHour"].source_ref == (
        "SAP:ZAERA_MIRROR_SRV/MaterialConsumptionRate(Material='MAT-48219',Plant='1010')"
        "/ConsumptionQuantityPerHour"
    )
    assert by_name["rar:2000451/10"].source_ref == (
        "SAP:API_SALES_ORDER_SRV/A_SalesOrderItem(SalesOrder='2000451',SalesOrderItem='10')"
        "/NetAmount"
    )
    assert all(f.source_ref.startswith("SAP:") for f in triage.figures)


def test_spare_part_sales_count_directly(sap: SapClient) -> None:
    triage = assess(sap, "MAT-51002", "1010", T0)

    assert triage.hours_to_stockout == Decimal(45)
    assert triage.rar_usd == Decimal(600_000)
    assert triage.priority_score == Decimal(1_200_000)


def test_orders_confirmed_before_the_stockout_day_are_not_at_risk(sap: SapClient) -> None:
    # MAT-72055 lasts 250 h (10.4 days); its only order is confirmed at T0 + 12 d.
    early = assess(sap, "MAT-72055", "1010", T0)
    assert early.rar_usd == Decimal(50_000)
    late = assess(sap, "MAT-72055", "1010", datetime(2026, 9, 20, tzinfo=UTC))
    assert late.stockout_at is not None and late.stockout_at.date().isoformat() == "2026-09-30"
    assert late.rar_usd == Decimal(50_000)


def test_the_reference_case_outranks_the_other_five(sap: SapClient) -> None:
    scores = {
        material: assess(sap, material, "1010", T0).priority_score
        for material in (
            "MAT-48219",
            "MAT-51002",
            "MAT-33871",
            "MAT-20114",
            "MAT-60417",
            "MAT-72055",
        )
    }
    assert max(scores, key=lambda m: scores[m]) == "MAT-48219"
