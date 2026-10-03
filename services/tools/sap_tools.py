"""Read-only SAP tools of SRD 6.3.2 (FR-IMP-01, IR-01..03). No tool here can write."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from services.shared.partners import load_contacts
from services.shared.sap_client import SapNotFoundError
from services.shared.sap_values import at, number, results
from services.shared.triage import (
    PRODUCTION,
    SALES,
    STOCK,
    consumption_rate,
    finished_products,
    odata_quote,
    sales_exposures,
)
from services.tools.context import (
    ToolContext,
    ToolError,
    decimal,
    json_dict,
    parse_time,
)

PO = "API_PURCHASEORDER_PROCESS_SRV"
PARTNER = "API_BUSINESS_PARTNER"


def sap_get_purchase_order(ctx: ToolContext, po_number: str) -> dict[str, Any]:
    try:
        record = ctx.sap.get(
            PO,
            "A_PurchaseOrder",
            {"PurchaseOrder": po_number},
            expand="to_PurchaseOrderItem/to_ScheduleLine",
            run_id=ctx.run_id,
        )
    except SapNotFoundError:
        raise ToolError(f"purchase order {po_number} does not exist in SAP") from None
    items = []
    for item in results(record.data.get("to_PurchaseOrderItem")):
        item_ref = (
            f"SAP:{PO}/A_PurchaseOrderItem(PurchaseOrder='{po_number}',"
            f"PurchaseOrderItem='{item['PurchaseOrderItem']}')"
        )
        lines = []
        for line in results(item.get("to_ScheduleLine")):
            lines.append(
                {
                    "scheduleLine": line["ScheduleLine"],
                    "deliveryAt": at(
                        line.get("ScheduleLineDeliveryDate"), line.get("ScheduleLineDeliveryTime")
                    ),
                    "quantity": number(line.get("ScheduleLineOrderQuantity")),
                    "sourceRef": (
                        f"SAP:{PO}/A_PurchaseOrderScheduleLine(PurchasingDocument='{po_number}',"
                        f"PurchasingDocumentItem='{item['PurchaseOrderItem']}',"
                        f"ScheduleLine='{line['ScheduleLine']}')"
                    ),
                }
            )
        items.append(
            {
                "item": item["PurchaseOrderItem"],
                "material": item.get("Material"),
                "text": item.get("PurchaseOrderItemText"),
                "plant": item.get("Plant"),
                "orderQuantity": number(item.get("OrderQuantity")),
                "unit": item.get("PurchaseOrderQuantityUnit"),
                "netPrice": number(item.get("NetPriceAmount")),
                "currency": item.get("DocumentCurrency"),
                "sourceRef": item_ref,
                "scheduleLines": lines,
            }
        )
    return json_dict(
        {
            "poNumber": po_number,
            "supplier": record.data.get("Supplier"),
            "sourceRef": record.source_ref,
            "items": items,
        }
    )


def stock_position(ctx: ToolContext, material: str, plant: str) -> dict[str, Any]:
    rows = ctx.sap.query(
        STOCK,
        "A_MatlStkInAcctMod",
        filter=f"Material eq {odata_quote(material)} and Plant eq {odata_quote(plant)}",
        run_id=ctx.run_id,
    )
    on_hand = sum((number(r.data.get("MatlWrhsStkQtyInMatlBaseUnit")) for r in rows), Decimal(0))
    per_hour, rate_ref = consumption_rate(ctx.sap, material, plant, ctx.now())
    return {
        "material": material,
        "plant": plant,
        "unrestricted": on_hand,
        "stockSourceRefs": [r.field_ref("MatlWrhsStkQtyInMatlBaseUnit") for r in rows],
        "consumptionPerHour": per_hour,
        "consumptionSourceRef": rate_ref if per_hour is not None else None,
    }


def sap_get_stock(ctx: ToolContext, material: str, plant: str) -> dict[str, Any]:
    position = stock_position(ctx, material, plant)
    per_hour = position["consumptionPerHour"]
    if per_hour:
        hours = position["unrestricted"] / per_hour
        position["hoursToStockout"] = hours.quantize(Decimal("0.1"))
        position["stockoutAt"] = ctx.now() + timedelta(seconds=int(hours * 3600))
    return json_dict(position)


def component_requirements(ctx: ToolContext, material: str, plant: str) -> list[dict[str, Any]]:
    rows = ctx.sap.query(
        PRODUCTION,
        "A_ProductionOrderComponent_2",
        filter=f"Material eq {odata_quote(material)} and Plant eq {odata_quote(plant)}",
        run_id=ctx.run_id,
    )
    requirements = []
    for row in rows:
        needed = at(
            row.data.get("MatlCompRequirementDate"), row.data.get("MatlCompRequirementTime")
        )
        open_qty = number(row.data.get("RequiredQuantity")) - number(
            row.data.get("WithdrawnQuantity")
        )
        if needed is None or open_qty <= 0:
            continue
        requirements.append(
            {
                "productionOrder": str(row.data["ManufacturingOrder"]),
                "requiredAt": needed,
                "openQuantity": open_qty,
                "sourceRef": row.field_ref("RequiredQuantity"),
            }
        )
    return sorted(requirements, key=lambda r: r["requiredAt"])


def sap_get_production_orders(
    ctx: ToolContext, material: str, plant: str, from_date: str, to_date: str
) -> dict[str, Any]:
    start = parse_time(from_date)
    end = parse_time(to_date)
    wanted = [
        r for r in component_requirements(ctx, material, plant) if start <= r["requiredAt"] <= end
    ]
    orders = []
    for requirement in wanted:
        record = ctx.sap.get(
            PRODUCTION,
            "A_ProductionOrder_2",
            {"ManufacturingOrder": requirement["productionOrder"]},
            run_id=ctx.run_id,
        )
        orders.append(
            {
                **requirement,
                "finishedMaterial": record.data.get("Material"),
                "orderQuantity": number(record.data.get("TotalQuantity")),
                "orderSourceRef": record.source_ref,
            }
        )
    return json_dict({"material": material, "plant": plant, "orders": orders})


def sap_get_sales_orders(
    ctx: ToolContext, material: str, plant: str, from_date: str, to_date: str
) -> dict[str, Any]:
    start = parse_time(from_date).date()
    end = parse_time(to_date).date()
    products = finished_products(ctx.sap, material, plant) | {material}
    exposures = [
        e for e in sales_exposures(ctx.sap, products, plant) if start <= e.confirmed <= end
    ]
    groups: dict[str, str] = {}
    for sales_order in sorted({e.sales_order for e in exposures}):
        header = ctx.sap.get(SALES, "A_SalesOrder", {"SalesOrder": sales_order}, run_id=ctx.run_id)
        groups[sales_order] = str(header.data.get("CustomerGroup") or "")
    return json_dict(
        {
            "material": material,
            "peggedMaterials": sorted(products),
            "items": [
                {
                    "salesOrder": e.sales_order,
                    "item": e.item,
                    "customerGroup": groups.get(e.sales_order),
                    "netUsd": e.net_usd,
                    "confirmedDate": e.confirmed,
                    "sourceRef": e.source_ref,
                }
                for e in sorted(exposures, key=lambda e: (e.confirmed, e.sales_order))
            ],
        }
    )


def sap_get_supplier(ctx: ToolContext, supplier_id: str) -> dict[str, Any]:
    try:
        record = ctx.sap.get(PARTNER, "A_Supplier", {"Supplier": supplier_id}, run_id=ctx.run_id)
    except SapNotFoundError:
        raise ToolError(f"supplier {supplier_id} does not exist in SAP") from None
    contact = next((c for c in load_contacts(ctx.sap) if c.partner_id == supplier_id), None)
    return json_dict(
        {
            "supplierId": supplier_id,
            "name": record.data.get("SupplierName"),
            "complianceStatus": record.data.get("ComplianceStatus"),
            "purchasingBlocked": bool(record.data.get("PurchasingIsBlocked")),
            "sourceRef": record.source_ref,
            "emailDomains": sorted(contact.email_domains) if contact else [],
            "kind": contact.kind if contact else None,
        }
    )


def find_sources(
    ctx: ToolContext, material: str, plant: str, qty_needed: Any, need_by: str
) -> dict[str, Any]:
    """Other plants with stock free above minimum cover (BR-07) and alternate suppliers."""
    needed = decimal(qty_needed, "qtyNeeded")
    deadline = parse_time(need_by)
    now = ctx.now()
    cover_days = ctx.config.decimal("DONOR_MIN_COVER_DAYS")
    rows = ctx.sap.query(
        STOCK,
        "A_MatlStkInAcctMod",
        filter=f"Material eq {odata_quote(material)}",
        run_id=ctx.run_id,
    )
    plants = sorted({str(r.data["Plant"]) for r in rows} - {plant})
    transfers = []
    for donor in plants:
        position = stock_position(ctx, material, donor)
        per_hour = position["consumptionPerHour"] or Decimal(0)
        minimum = per_hour * 24 * cover_days
        free = max(position["unrestricted"] - minimum, Decimal(0))
        rate = ctx.rates.find("STO", now.date(), from_plant=donor, to_plant=plant)
        if free <= 0 or rate is None:
            continue
        arrival = now + timedelta(hours=float(rate.lead_time_hours))
        transfers.append(
            {
                "actionType": "STO",
                "fromPlant": donor,
                "freeQuantity": free,
                "minimumCover": minimum,
                "minimumCoverDays": cover_days,
                "minimumCoverRule": "config:DONOR_MIN_COVER_DAYS",
                "arrival": arrival,
                "arrivesBeforeNeed": arrival <= deadline,
                "stockSourceRefs": position["stockSourceRefs"],
                "rateSourceRef": rate.source_ref,
            }
        )
    alternates = []
    for rate in ctx.rates.entries():
        if rate.action_type != "ALTERNATE_SUPPLIER" or not rate.valid_on(now.date()):
            continue
        supplier = sap_get_supplier(ctx, str(rate.supplier_id))
        arrival = now + timedelta(hours=float(rate.lead_time_hours))
        alternates.append(
            {
                "actionType": "ALTERNATE_SUPPLIER",
                "supplierId": rate.supplier_id,
                "name": supplier.get("name"),
                "complianceStatus": supplier.get("complianceStatus"),
                "arrival": arrival,
                "arrivesBeforeNeed": arrival <= deadline,
                "rateSourceRef": rate.source_ref,
            }
        )
    return json_dict(
        {
            "material": material,
            "plant": plant,
            "qtyNeeded": needed,
            "needBy": deadline,
            "transfers": transfers,
            "alternateSuppliers": alternates,
            "note": "Air freight of a supplier's ready partial: see calc_option AIR_FREIGHT.",
        }
    )
