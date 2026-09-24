"""FR-RPT-02/03: dashboard counts measured records with their source and basis."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from services.reporting.metrics import kpis
from services.shared.dynamo import to_item
from services.shared.models import Case, CaseStatus, Figure

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)


def stored_case(client: Any, number: int, status: CaseStatus, tier: Literal[1, 2, 3],
                revenue: int, minutes: int, *, sourced: bool = True) -> None:
    case = Case(
        case_id=f"EXC-2026-{number:04d}", type="MRP_EXCEPTION", material="MAT-48219",
        plant="1010", status=status, tier=tier, rar_usd=Decimal(revenue),
        figures=[Figure(name="rar:SO-1/10", value=Decimal(revenue), unit="USD",
                        source_ref="SAP:API_SALES_ORDER_SRV/A_SalesOrder/NetAmount")]
        if sourced else [],
        created_at=NOW, updated_at=NOW + timedelta(minutes=minutes),
    )
    client.put_item(TableName="aera-test-cases", Item=to_item({
        "PK": f"CASE#{case.case_id}", "SK": "META",
        **case.model_dump(mode="python", by_alias=True),
    }))


def test_fr_rpt_02_measured_kpis_keep_basis_sources_and_missing_costs(dynamodb: Any) -> None:
    stored_case(dynamodb, 914, CaseStatus.CLOSED, 1, 100, 30)
    stored_case(dynamodb, 915, CaseStatus.CLOSED, 2, 200, 60)
    stored_case(dynamodb, 916, CaseStatus.ESCALATED, 3, 999, 10)
    dynamodb.put_item(TableName="aera-test-signals", Item=to_item({
        "PK": "SIG#one", "SK": "META", "status": "QUARANTINED",
    }))

    values = kpis(dynamodb, "test")

    assert values["basis"] == "reference scenario (synthetic SAP Mirror)"
    assert values["revenueProtected"]["value"] == Decimal(300)
    assert values["revenueProtected"]["sampleSize"] == 2
    assert values["resolutionMedian"]["value"] == 45
    assert values["resolutionP95"]["value"] == 60
    assert values["touchlessRate"]["value"] == 0.5
    assert values["approvalsRequested"]["value"] == 1
    assert values["approvalsAvoided"]["value"] == 1
    assert values["tierDistribution"]["3"]["value"] == 1
    assert values["blockedSignals"]["value"] == 1
    assert values["costPerCase"]["value"] is None
    assert values["optimiserSavings"]["value"] is None
    for key in ("caseCount", "revenueProtected", "resolutionMedian", "resolutionP95",
                "touchlessRate", "approvalsRequested", "approvalsAvoided", "blockedSignals"):
        assert values[key]["sourceRef"] and values[key]["sampleSize"] >= 0


def test_fr_rpt_03_unsourced_revenue_is_excluded(dynamodb: Any) -> None:
    stored_case(dynamodb, 914, CaseStatus.CLOSED, 1, 500, 30, sourced=False)

    revenue = kpis(dynamodb, "test")["revenueProtected"]

    assert revenue["value"] == 0 and revenue["sampleSize"] == 0
