"""FR-RPT-02/03: source-linked KPIs from operational records only."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from math import ceil
from statistics import median
from typing import Any

from services.shared.dynamo import from_item, table_name
from services.shared.models import Case, CaseStatus


def _scan(client: Any, table: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    request: dict[str, Any] = {"TableName": table}
    while True:
        page = client.scan(**request)
        rows.extend(from_item(item, keep_decimals=False) for item in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return rows
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _value(
    value: int | float | Decimal | None, unit: str, source: str, count: int
) -> dict[str, Any]:
    return {"value": value, "unit": unit, "sourceRef": source, "sampleSize": count}


def _sourced_rar(case: Case) -> Decimal | None:
    figures = [f for f in case.figures if f.name.startswith("rar:")]
    if case.rar_usd is None or not figures or any(
        not f.source_ref.startswith("SAP:") for f in figures
    ):
        return None
    try:
        total = sum((Decimal(str(f.value)) for f in figures), Decimal(0))
    except InvalidOperation:
        return None
    return total if total.is_finite() and total == case.rar_usd else None


def kpis(client: Any, env: str) -> dict[str, Any]:
    case_table = table_name("cases", env)
    signal_table = table_name("signals", env)
    cases = []
    for item in _scan(client, case_table):
        if not str(item.get("PK", "")).startswith("CASE#") or item.get("SK") != "META":
            continue
        for key in ("PK", "SK", "stage"):
            item.pop(key, None)
        cases.append(Case.model_validate(item))
    signals = _scan(client, signal_table)
    closed = [case for case in cases if case.status is CaseStatus.CLOSED]
    durations = sorted(
        (case.updated_at - case.created_at).total_seconds() / 60
        for case in closed if case.updated_at >= case.created_at
    )
    sourced = [value for case in closed if (value := _sourced_rar(case)) is not None]
    tiers = Counter(str(case.tier) for case in cases if case.tier is not None)
    blocked = sum(item.get("status") == "QUARANTINED" for item in signals)
    case_ref = f"DynamoDB:{case_table}/CASE#*/META"
    signal_ref = f"DynamoDB:{signal_table}/SIG#*/META"
    return {
        "basis": "reference scenario (synthetic SAP Mirror)",
        "asOf": datetime.now(UTC).isoformat(),
        "caseCount": _value(len(cases), "cases", case_ref, len(cases)),
        "revenueProtected": _value(
            sum(sourced, Decimal(0)),
            "USD", case_ref + "/rarUsd (closed, SAP-sourced)", len(sourced),
        ),
        "resolutionMedian": _value(median(durations) if durations else None,
                                   "minutes", case_ref + "/createdAt,updatedAt", len(durations)),
        "resolutionP95": _value(
            durations[ceil(0.95 * len(durations)) - 1] if durations else None,
            "minutes", case_ref + "/createdAt,updatedAt", len(durations),
        ),
        "touchlessRate": _value(
            sum(case.tier == 1 for case in closed) / len(closed) if closed else None,
            "ratio", case_ref + "/tier,status", len(closed),
        ),
        "approvalsRequested": _value(tiers["2"], "cases", case_ref + "/tier", len(cases)),
        "approvalsAvoided": _value(tiers["1"], "cases", case_ref + "/tier", len(cases)),
        "tierDistribution": {tier: _value(tiers[tier], "cases", case_ref + "/tier", len(cases))
                             for tier in ("1", "2", "3")},
        "blockedSignals": _value(blocked, "signals", signal_ref + "/status", len(signals)),
        "costPerCase": _value(None, "USD", "UNMEASURED:model-cost", 0),
        "optimiserSavings": _value(None, "USD", "UNMEASURED:portfolio-runtime", 0),
    }
