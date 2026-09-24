"""Deterministic impact and option calculators (FR-IMP-02, FR-IMP-04, FR-OPT-02, BR-01, BR-02).

The model never computes a figure. `calc_impact` derives stock-out, units, orders and revenue
at risk from SAP; `calc_option` prices one action from the rate card. Each option result is
recorded as a draft on the case, and `propose_plan` accepts only options whose cost and
coverage match a draft (services/tools/case_tools.py).
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

from services.rules.br_02 import usable
from services.shared.dynamo import table_name, to_item
from services.shared.models import ExtractedField, FieldStatus
from services.shared.ratecard import Rate
from services.shared.triage import assess
from services.tools.context import (
    ToolContext,
    ToolError,
    decimal,
    json_dict,
    jsonable,
    parse_time,
)
from services.tools.reliability import profile_for
from services.tools.sap_tools import (
    component_requirements,
    sap_get_purchase_order,
    stock_position,
)

ACTION_TYPES = ("STO", "AIR_FREIGHT", "ALTERNATE_SUPPLIER")


def _case(ctx: ToolContext, case_id: str) -> Any:
    case = ctx.cases.get(case_id)
    if case is None:
        raise ToolError(f"case {case_id} does not exist")
    return case


def _fields(ctx: ToolContext, case_id: str) -> list[ExtractedField]:
    return [
        field
        for signal in ctx.signals.for_case(case_id)
        if signal.status.value == "ACCEPTED"
        for field in signal.fields
    ]


def _discrepancies(ctx: ToolContext, case: Any) -> list[dict[str, Any]]:
    """FR-IMP-04: where a signal and SAP disagree, SAP is used and the difference recorded."""
    if not case.po_number:
        return []
    po = sap_get_purchase_order(ctx, case.po_number)
    item = next((i for i in po["items"] if i.get("material") == case.material), None)
    if item is None:
        return []
    line = (item.get("scheduleLines") or [{}])[0]
    truth = {
        "QUANTITY": (str(item["orderQuantity"]), item["sourceRef"] + "/OrderQuantity"),
        "PRICE": (str(item["netPrice"]), item["sourceRef"] + "/NetPriceAmount"),
        "DELIVERY_DATE": (
            str(line.get("deliveryAt", ""))[:10],
            str(line.get("sourceRef", "")) + "/ScheduleLineDeliveryDate",
        ),
    }
    found = []
    for field in _fields(ctx, case.case_id):
        if field.name not in truth or field.status is FieldStatus.SAP_MATCHED:
            continue
        sap_value, sap_ref = truth[field.name]
        found.append(
            {
                "fieldId": field.field_id,
                "name": field.name,
                "signalValue": field.value,
                "signalStatus": field.status.value,
                "sapValue": sap_value,
                "sapSourceRef": sap_ref,
                "used": "SAP",
            }
        )
    return found


def calc_impact(
    ctx: ToolContext,
    case_id: str,
    recovery_at: str | None = None,
    recovery_source_ref: str | None = None,
) -> dict[str, Any]:
    result = compute_impact(ctx, case_id, recovery_at, recovery_source_ref)
    table = table_name("cases", ctx.env)
    for entry in result["discrepancies"]:
        ctx.dynamodb.put_item(
            TableName=table,
            Item=to_item({"PK": f"CASE#{case_id}", "SK": f"DISC#{entry['fieldId']}", **entry}),
        )
    ctx.dynamodb.put_item(
        TableName=table, Item=to_item({"PK": f"CASE#{case_id}", "SK": "IMPACT", **jsonable(result)})
    )
    return json_dict(result)


def compute_impact(
    ctx: ToolContext,
    case_id: str,
    recovery_at: str | None = None,
    recovery_source_ref: str | None = None,
) -> dict[str, Any]:
    """The impact from fresh SAP reads, without recording it (the Verifier re-derives it)."""
    case = _case(ctx, case_id)
    now = ctx.now()
    recovery = parse_time(recovery_at) if recovery_at else None
    if recovery is not None and not recovery_source_ref:
        raise ToolError("recoveryAt needs recoverySourceRef (the SAP line or signal it came from)")
    triage = assess(
        ctx.sap,
        case.material,
        case.plant,
        now,
        recovered_on=recovery.date() if recovery else None,
    )
    remaining = triage.on_hand
    units_at_risk = Decimal(0)
    orders: list[dict[str, Any]] = []
    for requirement in component_requirements(ctx, case.material, case.plant):
        if requirement["requiredAt"] < now:
            continue
        if recovery is not None and requirement["requiredAt"] >= recovery:
            break
        covered = min(remaining, requirement["openQuantity"])
        remaining -= covered
        short = requirement["openQuantity"] - covered
        if short > 0:
            units_at_risk += short
            orders.append({**requirement, "unitsShort": short})
    line_stop_hours = (
        (units_at_risk / triage.per_hour).quantize(Decimal("0.1")) if triage.per_hour else None
    )
    figures = [f.model_dump(mode="json", by_alias=True) for f in triage.figures]
    if recovery is not None:
        figures.append(
            {"name": "recoveryAt", "value": str(recovery), "sourceRef": recovery_source_ref}
        )
    result = {
        "caseId": case_id,
        "material": case.material,
        "plant": case.plant,
        "onHand": triage.on_hand,
        "consumptionPerHour": triage.per_hour,
        "hoursToStockout": (
            triage.hours_to_stockout.quantize(Decimal("0.1"))
            if triage.hours_to_stockout is not None
            else None
        ),
        "stockoutAt": triage.stockout_at,
        "recoveryAt": recovery,
        "unitsAtRisk": units_at_risk,
        "productionOrdersAtRisk": orders,
        "salesOrdersAtRisk": [
            {
                "salesOrder": e.sales_order,
                "item": e.item,
                "netUsd": e.net_usd,
                "confirmedDate": e.confirmed,
                "sourceRef": e.source_ref,
            }
            for e in triage.exposures
        ],
        "rarUsd": triage.rar_usd,
        "lineStopHours": line_stop_hours,
        # FR-IMP-02 downtime cost needs a cost rate SAP does not hold; none is configured.
        "downtimeCostUsd": None,
        "downtimeCostNote": "No downtime cost rate is configured; revenue at risk stands for it.",
        "discrepancies": _discrepancies(ctx, case),
        "figures": figures,
    }
    return result


def _usable_quantity(ctx: ToolContext, case_id: str, field_id: str) -> tuple[Decimal, str]:
    for field in _fields(ctx, case_id):
        if field.field_id != field_id:
            continue
        if field.name != "QUANTITY":
            raise ToolError(f"field {field_id} is {field.name}, not a quantity")
        if not usable(field):
            raise ToolError(
                f"field {field_id} is UNCONFIRMED (confidence {field.confidence}); "
                "ask the planner with ask_planner before using it (BR-02)"
            )
        reference = (
            f"planner:{field.confirmed_by.removeprefix('user:')}"
            if field.confirmed_by
            else f"signal:{field.signal_id}/{field.name}"
        )
        return decimal(field.value.replace(",", ""), "quantity"), reference
    raise ToolError(f"field {field_id} is not evidence of case {case_id}")


def _plain(value: Any) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def draft_key(cost: Any, coverage: Any, cost_ref: str) -> str:
    canonical = json.dumps(
        {"cost": _plain(cost), "coverage": _plain(coverage), "ref": cost_ref}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def calc_option(
    ctx: ToolContext, case_id: str, action_type: str, params: dict[str, Any]
) -> dict[str, Any]:
    draft, _ = compute_option(ctx, case_id, action_type, params)
    ctx.dynamodb.put_item(
        TableName=table_name("cases", ctx.env),
        Item=to_item(
            {
                "PK": f"CASE#{case_id}",
                "SK": "DRAFT#"
                + draft_key(draft["costUsd"], draft["coverageUnits"], draft["costSourceRef"]),
                **jsonable(draft),
                # What the Verifier needs to recalculate the option independently.
                "params": jsonable(params),
                "createdAt": ctx.now().isoformat(),
            }
        ),
    )
    return json_dict(draft)


def compute_option(
    ctx: ToolContext, case_id: str, action_type: str, params: dict[str, Any]
) -> tuple[dict[str, Any], Rate]:
    """One priced option from fresh SAP and rate-card reads, without recording it."""
    case = _case(ctx, case_id)
    now = ctx.now()
    if action_type not in ACTION_TYPES:
        raise ToolError(f"actionType must be one of {', '.join(ACTION_TYPES)}")
    figures: list[dict[str, Any]] = []
    if action_type == "STO":
        donor = str(params.get("fromPlant") or "")
        qty = decimal(params.get("qty"), "qty")
        rate = ctx.rates.find("STO", now.date(), from_plant=donor, to_plant=case.plant)
        if rate is None:
            raise ToolError(f"no rate card entry for a transfer {donor} -> {case.plant}")
        position = stock_position(ctx, case.material, donor)
        per_hour = position["consumptionPerHour"] or Decimal(0)
        free = position["unrestricted"] - per_hour * 24 * ctx.config.decimal("DONOR_MIN_COVER_DAYS")
        if qty > free:
            raise ToolError(f"plant {donor} has only {free} free above minimum cover (BR-07)")
        arrival = now + timedelta(hours=float(rate.lead_time_hours))
        actions: list[dict[str, Any]] = [
            {
                "type": "CREATE_STO",
                "fromPlant": donor,
                "toPlant": case.plant,
                "material": case.material,
                "qty": qty,
                "deliveryDate": arrival.date(),
            }
        ]
        figures += [
            {"name": "donorFree", "value": free, "unit": "PC", "sourceRef": ref}
            for ref in position["stockSourceRefs"][:1]
        ]
        coverage = qty
    elif action_type == "AIR_FREIGHT":
        if not case.po_number:
            raise ToolError("air freight needs the case's purchase order")
        po = sap_get_purchase_order(ctx, case.po_number)
        supplier = str(params.get("supplierId") or po["supplier"])
        field_id = params.get("qtyFieldId")
        if field_id:
            qty, qty_ref = _usable_quantity(ctx, case_id, str(field_id))
        else:
            qty = decimal(params.get("qty"), "qty")
            qty_ref = str(params.get("qtySourceRef") or "")
            if not qty_ref.startswith(("planner:", "SAP:")):
                raise ToolError(
                    "the air-freighted quantity must come from a confirmed field (qtyFieldId) "
                    "or a planner or SAP source (qtySourceRef)"
                )
        rate = ctx.rates.find("AIR_FREIGHT", now.date(), supplier_id=supplier)
        if rate is None:
            raise ToolError(f"no air freight rate for supplier {supplier}")
        item = next((i for i in po["items"] if i.get("material") == case.material), po["items"][0])
        arrival = now + timedelta(hours=float(rate.lead_time_hours))
        actions = [
            {
                "type": "BOOK_AIR_FREIGHT",
                "supplierId": supplier,
                "poNumber": case.po_number,
                "poItem": item["item"],
                "qty": qty,
                "arrival": arrival,
            }
        ]
        if params.get("remainderAt"):
            remainder_at = parse_time(str(params["remainderAt"]))
            ordered = decimal(item["orderQuantity"], "orderQuantity")
            if qty >= ordered:
                raise ToolError("nothing remains to split: the partial covers the whole item")
            line = (item.get("scheduleLines") or [{"scheduleLine": "1"}])[0]
            actions.append(
                {
                    "type": "SPLIT_PO_SCHEDULE_LINE",
                    "poNumber": case.po_number,
                    "poItem": item["item"],
                    "scheduleLine": str(line["scheduleLine"]),
                    "parts": [
                        {"qty": qty, "deliveryDate": arrival.date()},
                        {"qty": ordered - qty, "deliveryDate": remainder_at.date()},
                    ],
                }
            )
            figures.append(
                {
                    "name": "remainderAt",
                    "value": str(remainder_at),
                    "sourceRef": str(params.get("remainderSourceRef") or item["sourceRef"]),
                }
            )
        figures.append({"name": "airQuantity", "value": qty, "unit": "PC", "sourceRef": qty_ref})
        coverage = qty
    else:
        supplier = str(params.get("supplierId") or "")
        qty = decimal(params.get("qty"), "qty")
        rate = ctx.rates.find("ALTERNATE_SUPPLIER", now.date(), supplier_id=supplier)
        if rate is None:
            raise ToolError(f"no alternate-supplier rate for {supplier}")
        arrival = now + timedelta(hours=float(rate.lead_time_hours))
        promised_arrival = arrival
        profile = profile_for(ctx, supplier, case.material)
        if profile is not None:
            delay_days = int(profile["p90DelayDays"])
            arrival += timedelta(days=delay_days)
            source_ref = str(profile["sourceRefs"][0])
            figures.extend([
                {"name": "supplierP90DelayDays", "value": delay_days,
                 "unit": "days", "sourceRef": source_ref},
                {"name": "supplierSampleSize", "value": profile["sampleSize"],
                 "unit": "schedule lines", "sourceRef": source_ref},
            ])
        actions = [
            {
                "type": "CREATE_PO_ALTERNATE",
                "supplierId": supplier,
                "material": case.material,
                "plant": case.plant,
                "qty": qty,
                "deliveryDate": promised_arrival.date(),
            }
        ]
        coverage = qty
    cost = rate.cost(coverage)
    figures.append({"name": "costUsd", "value": cost, "unit": "USD", "sourceRef": rate.source_ref})
    figures.append({"name": "arrival", "value": str(arrival), "sourceRef": rate.source_ref})
    draft = {
        "actionType": action_type,
        "coverageUnits": coverage,
        "arrival": arrival,
        "costUsd": cost,
        "costSourceRef": rate.source_ref,
        "actions": actions,
        "figures": figures,
    }
    return draft, rate


def draft_exists(ctx: ToolContext, case_id: str, cost: Any, coverage: Any, cost_ref: str) -> bool:
    item = ctx.dynamodb.get_item(
        TableName=table_name("cases", ctx.env),
        Key={
            "PK": {"S": f"CASE#{case_id}"},
            "SK": {"S": f"DRAFT#{draft_key(cost, coverage, cost_ref)}"},
        },
    ).get("Item")
    return item is not None
