"""FR-LRN-01: join SAP PO schedule lines to goods-receipt posting history."""

from __future__ import annotations

from datetime import datetime, timedelta

from services.dialogue.reliability import Receipt, Schedule
from services.shared.sap_client import SapClient
from services.shared.sap_values import edm_date, number, results
from services.shared.triage import odata_quote

PO = "API_PURCHASEORDER_PROCESS_SRV"
DOCUMENTS = "API_MATERIAL_DOCUMENT_SRV"


def load_history(
    sap: SapClient,
    supplier_id: str,
    material: str,
    now: datetime,
    *,
    window_days: int = 180,
) -> list[Schedule]:
    if not supplier_id or not material or now.tzinfo is None or window_days <= 0:
        raise ValueError("supplier history needs supplier, material and aware time")
    first = now.date() - timedelta(days=window_days)
    orders = sap.query(
        PO,
        "A_PurchaseOrder",
        filter=f"Supplier eq {odata_quote(supplier_id)}",
        expand="to_PurchaseOrderItem/to_ScheduleLine",
    )
    history: list[Schedule] = []
    for order in orders:
        po_number = str(order.data["PurchaseOrder"])
        for item in results(order.data.get("to_PurchaseOrderItem")):
            if item.get("Material") != material:
                continue
            item_number = str(item["PurchaseOrderItem"])
            lines = [
                (due, line)
                for line in results(item.get("to_ScheduleLine"))
                if (due := edm_date(line.get("ScheduleLineDeliveryDate")))
                and first <= due < now.date()
            ]
            if not lines:
                continue
            documents = sap.query(
                DOCUMENTS,
                "A_MaterialDocumentItem",
                filter=(
                    f"PurchaseOrder eq {odata_quote(po_number)} and "
                    f"PurchaseOrderItem eq {odata_quote(item_number)} and "
                    "GoodsMovementType eq '101'"
                ),
            )
            receipts: list[Receipt] = []
            for document in documents:
                header = sap.get(
                    DOCUMENTS,
                    "A_MaterialDocumentHeader",
                    {
                        "MaterialDocumentYear": str(document.data["MaterialDocumentYear"]),
                        "MaterialDocument": str(document.data["MaterialDocument"]),
                    },
                )
                posted = edm_date(header.data.get("PostingDate"))
                if posted is None:
                    raise ValueError("SAP receipt has no posting date")
                if posted <= now.date():
                    receipts.append(
                        Receipt(
                            posted,
                            number(document.data.get("QuantityInBaseUnit")),
                            f"{document.source_ref}/QuantityInBaseUnit + "
                            f"{header.source_ref}/PostingDate",
                        )
                    )
            available = sorted(receipts, key=lambda r: (r.posted_on, r.source_ref))
            for due, line in sorted(
                lines, key=lambda pair: (pair[0], str(pair[1]["ScheduleLine"]))
            ):
                required = number(line.get("ScheduleLineOrderQuantity"))
                assigned: list[Receipt] = []
                remaining = required
                rest: list[Receipt] = []
                for receipt in available:
                    take = min(remaining, receipt.quantity)
                    if take > 0:
                        assigned.append(Receipt(receipt.posted_on, take, receipt.source_ref))
                        remaining -= take
                    if receipt.quantity > take:
                        rest.append(
                            Receipt(receipt.posted_on, receipt.quantity - take, receipt.source_ref)
                        )
                available = rest
                history.append(
                    Schedule(
                        supplier_id,
                        material,
                        due,
                        required,
                        tuple(assigned),
                        f"{order.source_ref}/to_PurchaseOrderItem({item_number})/"
                        f"to_ScheduleLine({line['ScheduleLine']})/ScheduleLineOrderQuantity",
                    )
                )
    return history
