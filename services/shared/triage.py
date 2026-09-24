"""Triage figures for one material at one plant, read from SAP (FR-TRI-01, FR-IMP-03, BR-13).

- On-hand stock: `API_MATERIAL_STOCK_SRV/A_MatlStkInAcctMod`.
- Consumption per hour: the Mirror's `MaterialConsumptionRate` (SRD 6.6.3); where a system
  has none, the component requirements of the next 24 hours.
- Stock-out: on-hand / consumption from `now`.
- Revenue at risk: sales order items for the material itself and for every finished product
  whose production orders consume it (pegging through production order components), counted
  from the stock-out day on (services/rules/impact.py).

Every figure carries the `sourceRef` of the SAP record it came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from services.rules.br_13 import priority_score
from services.rules.impact import SalesExposure, hours_to_stockout, revenue_at_risk
from services.shared.models import Figure
from services.shared.sap_client import SapClient, SapNotFoundError
from services.shared.sap_values import at, edm_date, number, results

STOCK = "API_MATERIAL_STOCK_SRV"
MIRROR = "ZAERA_MIRROR_SRV"
PRODUCTION = "API_PRODUCTION_ORDER_2_SRV"
SALES = "API_SALES_ORDER_SRV"


@dataclass(frozen=True)
class Triage:
    material: str
    plant: str
    on_hand: Decimal
    per_hour: Decimal | None
    hours_to_stockout: Decimal | None
    stockout_at: datetime | None
    rar_usd: Decimal
    priority_score: Decimal
    exposures: list[SalesExposure] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _consumption(
    sap: SapClient, material: str, plant: str, now: datetime
) -> tuple[Decimal | None, str]:
    try:
        rate = sap.get(MIRROR, "MaterialConsumptionRate", {"Material": material, "Plant": plant})
        return number(rate.data.get("ConsumptionQuantityPerHour")), rate.field_ref(
            "ConsumptionQuantityPerHour"
        )
    except SapNotFoundError:
        pass
    components = sap.query(
        PRODUCTION,
        "A_ProductionOrderComponent_2",
        filter=f"Material eq {_quote(material)} and Plant eq {_quote(plant)}",
    )
    horizon = now + timedelta(hours=24)
    due = Decimal(0)
    for row in components:
        needed = at(
            row.data.get("MatlCompRequirementDate"), row.data.get("MatlCompRequirementTime")
        )
        if needed is not None and now <= needed < horizon:
            due += number(row.data.get("RequiredQuantity")) - number(
                row.data.get("WithdrawnQuantity")
            )
    if due <= 0:
        return None, f"SAP:{PRODUCTION}/A_ProductionOrderComponent_2"
    return due / 24, f"SAP:{PRODUCTION}/A_ProductionOrderComponent_2"


def _finished_products(sap: SapClient, material: str, plant: str) -> set[str]:
    components = sap.query(
        PRODUCTION,
        "A_ProductionOrderComponent_2",
        filter=f"Material eq {_quote(material)} and Plant eq {_quote(plant)}",
        select="ManufacturingOrder",
    )
    orders = {str(row.data["ManufacturingOrder"]) for row in components}
    products: set[str] = set()
    for order in sorted(orders):
        record = sap.get(PRODUCTION, "A_ProductionOrder_2", {"ManufacturingOrder": order})
        products.add(str(record.data["Material"]))
    return products


def _exposures(sap: SapClient, materials: set[str], plant: str) -> list[SalesExposure]:
    exposures: list[SalesExposure] = []
    for material in sorted(materials):
        items = sap.query(
            SALES,
            "A_SalesOrderItem",
            filter=f"Material eq {_quote(material)} and ProductionPlant eq {_quote(plant)}",
            expand="to_ScheduleLine",
        )
        for item in items:
            if item.data.get("TransactionCurrency") not in (None, "", "USD"):
                continue  # RaR is in USD; other currencies need a rate the SRD does not give
            confirmed = [
                edm_date(line.get("ConfirmedDeliveryDate"))
                for line in results(item.data.get("to_ScheduleLine"))
            ]
            dates = [d for d in confirmed if d is not None]
            if not dates:
                continue
            exposures.append(
                SalesExposure(
                    sales_order=str(item.data["SalesOrder"]),
                    item=str(item.data["SalesOrderItem"]),
                    net_usd=number(item.data.get("NetAmount")),
                    confirmed=min(dates),
                    source_ref=item.field_ref("NetAmount"),
                )
            )
    return exposures


def assess(
    sap: SapClient,
    material: str,
    plant: str,
    now: datetime,
    *,
    recovered_on: date | None = None,
) -> Triage:
    stock_rows = sap.query(
        STOCK,
        "A_MatlStkInAcctMod",
        filter=f"Material eq {_quote(material)} and Plant eq {_quote(plant)}",
    )
    on_hand = sum(
        (number(r.data.get("MatlWrhsStkQtyInMatlBaseUnit")) for r in stock_rows), Decimal(0)
    )
    stock_ref = (
        stock_rows[0].field_ref("MatlWrhsStkQtyInMatlBaseUnit")
        if len(stock_rows) == 1
        else f"SAP:{STOCK}/A_MatlStkInAcctMod"
    )
    per_hour, rate_ref = _consumption(sap, material, plant, now)
    hours = None if per_hour is None else hours_to_stockout(on_hand, per_hour)
    stockout = None if hours is None else now + timedelta(seconds=int(hours * 3600))

    products = _finished_products(sap, material, plant) | {material}
    total, threatened = revenue_at_risk(
        _exposures(sap, products, plant), stockout_at=stockout, recovered_on=recovered_on
    )
    score = priority_score(total, hours)

    figures = [
        Figure(name="onHand", value=on_hand, unit="PC", source_ref=stock_ref, read_at=now),
    ]
    if per_hour is not None:
        figures.append(
            Figure(
                name="consumptionPerHour",
                value=per_hour,
                unit="PC/h",
                source_ref=rate_ref,
                read_at=now,
            )
        )
        if hours is not None:
            figures.append(
                Figure(
                    name="hoursToStockout",
                    value=hours.quantize(Decimal("0.1")),
                    unit="h",
                    source_ref=rate_ref,
                    read_at=now,
                )
            )
    figures += [
        Figure(
            name=f"rar:{e.sales_order}/{e.item}",
            value=e.net_usd,
            unit="USD",
            source_ref=e.source_ref,
            read_at=now,
        )
        for e in threatened
    ]
    return Triage(
        material=material,
        plant=plant,
        on_hand=on_hand,
        per_hour=per_hour,
        hours_to_stockout=hours,
        stockout_at=stockout,
        rar_usd=total,
        priority_score=score,
        exposures=threatened,
        figures=figures,
    )
