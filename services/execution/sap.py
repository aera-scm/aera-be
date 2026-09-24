"""Mirror adapter for reversible STO and schedule-date operations (IR-02)."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from services.execution.workflow import Execution, Step
from services.shared.sap_client import SapClient, SapNotFoundError, SapRecord, Target
from services.shared.triage import odata_quote

PO = "API_PURCHASEORDER_PROCESS_SRV"
LINE = "A_PurchaseOrderScheduleLine"
ITEM = "A_PurchaseOrderItem"


def sap_date(value: date) -> str:
    epoch = datetime(value.year, value.month, value.day, tzinfo=UTC)
    return f"/Date({int(epoch.timestamp() * 1000)})/"


def etag(record: SapRecord) -> str:
    if not record.etag:
        raise ValueError("SAP entity has no ETag; write refused")
    return record.etag


class SapBoundary:
    def __init__(
        self,
        sap: SapClient,
        *,
        authorize: Callable[[Execution], bool],
        authorize_rollback: Callable[[Execution], bool],
        kill_switch: Callable[[], bool],
        revalidate: Callable[[Execution], bool],
        audit: Callable[[str, dict[str, Any]], None],
        sto_header: dict[str, str],
    ) -> None:
        if sap.write is None or sap.write.target != Target.MIRROR or sap.read != sap.write:
            raise ValueError("execution requires matching Mirror read/write endpoints")
        required = {"CompanyCode", "PurchasingOrganization", "PurchasingGroup", "DocumentCurrency"}
        if set(sto_header) != required or any(not value for value in sto_header.values()):
            raise ValueError("trusted SAP purchasing context required")
        self.sap = sap
        self.authorize = authorize
        self.authorize_rollback = authorize_rollback
        self.kill_switch = kill_switch
        self.revalidate = revalidate
        self.audit = audit
        self.header = dict(sto_header)
        self.prepared: dict[tuple[int, str], dict[str, Any]] = {}

    def prepare_undo(self, step: Step) -> dict[str, Any]:
        action = step.action
        if action.type == "CREATE_STO":
            material = self.sap.get(
                "ZAERA_MIRROR_SRV",
                "MaterialConsumptionRate",
                {"Material": action.material, "Plant": action.from_plant},
            )
            unit = material.data.get("MaterialBaseUnit")
            if not isinstance(unit, str) or not unit:
                raise ValueError("material base unit unavailable")
            undo = {
                "type": "DELETE_STO_ITEM",
                "beforeGoodsIssue": True,
                "baseUnit": unit,
                "unitSourceRef": material.field_ref("MaterialBaseUnit"),
            }
        elif action.type == "CHANGE_PO_DATE":
            keys = {
                "PurchasingDocument": action.po_number,
                "PurchasingDocumentItem": action.po_item,
                "ScheduleLine": action.schedule_line,
            }
            current = self.sap.get(PO, LINE, keys)
            undo = {
                "type": "RESTORE_DATE",
                "keys": keys,
                "value": current.data["ScheduleLineDeliveryDate"],
                "etag": etag(current),
            }
        elif action.type == "SPLIT_PO_SCHEDULE_LINE":
            keys = {
                "PurchasingDocument": action.po_number,
                "PurchasingDocumentItem": action.po_item,
                "ScheduleLine": str(int(action.schedule_line) + step.substep),
            }
            if step.substep == 0:
                current = self.sap.get(PO, LINE, keys)
                if sum(part.qty for part in action.parts) != Decimal(
                    str(current.data["ScheduleLineOrderQuantity"])
                ):
                    raise ValueError("split must preserve original schedule-line quantity")
                undo = {
                    "type": "RESTORE_LINE",
                    "keys": keys,
                    "quantity": current.data["ScheduleLineOrderQuantity"],
                    "value": current.data["ScheduleLineDeliveryDate"],
                    "etag": etag(current),
                }
            else:
                try:
                    self.sap.get(PO, LINE, keys)
                except SapNotFoundError:
                    pass
                else:
                    raise ValueError("split target schedule line already exists")
                undo = {"type": "DELETE_LINE", "keys": keys}
        else:
            raise ValueError(f"{action.type} needs a durable multi-write adapter before execution")
        self.prepared[(step.index, step.target)] = undo
        return undo

    def apply(self, step: Step) -> dict[str, Any]:
        action = step.action
        undo = self.prepared[(step.index, step.target)]
        if action.type == "CREATE_STO":
            record = self.sap.create(
                PO,
                "A_PurchaseOrder",
                {
                    **self.header,
                    "PurchaseOrderType": "UB",
                    "SupplyingPlant": action.from_plant,
                    "to_PurchaseOrderItem": [
                        {
                            "PurchaseOrderItem": "10",
                            "Plant": action.to_plant,
                            "Material": action.material,
                            "OrderQuantity": str(action.qty),
                            "PurchaseOrderQuantityUnit": undo["baseUnit"],
                            "to_ScheduleLine": [
                                {
                                    "ScheduleLine": "1",
                                    "ScheduleLineDeliveryDate": sap_date(action.delivery_date),
                                    "ScheduleLineOrderQuantity": str(action.qty),
                                }
                            ],
                        }
                    ],
                },
            )
            return {"document": record.data["PurchaseOrder"], "sourceRef": record.source_ref}
        if action.type == "CHANGE_PO_DATE":
            self.sap.update(
                PO,
                LINE,
                undo["keys"],
                {"ScheduleLineDeliveryDate": sap_date(action.new_date)},
                etag=undo["etag"],
            )
            current = self.sap.get(PO, LINE, undo["keys"])
            return {
                "document": action.po_number,
                "sourceRef": current.source_ref,
                "afterEtag": etag(current),
            }
        if action.type == "SPLIT_PO_SCHEDULE_LINE":
            part = action.parts[step.substep]
            changes = {
                "ScheduleLineOrderQuantity": str(part.qty),
                "ScheduleLineDeliveryDate": sap_date(part.delivery_date),
            }
            if step.substep == 0:
                self.sap.update(PO, LINE, undo["keys"], changes, etag=undo["etag"])
                current = self.sap.get(PO, LINE, undo["keys"])
            else:
                current = self.sap.create_under(
                    PO,
                    ITEM,
                    {"PurchaseOrder": action.po_number, "PurchaseOrderItem": action.po_item},
                    "to_ScheduleLine",
                    {**changes, "ScheduleLine": undo["keys"]["ScheduleLine"]},
                )
            return {"document": action.po_number, "sourceRef": current.source_ref}
        raise ValueError("unsupported action")

    def verify(self, step: Step, result: dict[str, Any]) -> bool:
        action = step.action
        if action.type == "CREATE_STO":
            header = self.sap.get(PO, "A_PurchaseOrder", {"PurchaseOrder": result["document"]})
            item = self.sap.get(
                PO, ITEM, {"PurchaseOrder": result["document"], "PurchaseOrderItem": "10"}
            )
            line = self.sap.get(
                PO,
                LINE,
                {
                    "PurchasingDocument": result["document"],
                    "PurchasingDocumentItem": "10",
                    "ScheduleLine": "1",
                },
            )
            return (
                header.data.get("PurchaseOrderType") == "UB"
                and header.data.get("SupplyingPlant") == action.from_plant
                and item.data.get("Material") == action.material
                and item.data.get("Plant") == action.to_plant
                and Decimal(str(item.data.get("OrderQuantity"))) == action.qty
                and line.data.get("ScheduleLineDeliveryDate") == sap_date(action.delivery_date)
                and Decimal(str(line.data.get("ScheduleLineOrderQuantity"))) == action.qty
            )
        if action.type == "CHANGE_PO_DATE":
            record = self.sap.get(
                PO,
                LINE,
                {
                    "PurchasingDocument": action.po_number,
                    "PurchasingDocumentItem": action.po_item,
                    "ScheduleLine": action.schedule_line,
                },
            )
            return record.data.get("ScheduleLineDeliveryDate") == sap_date(action.new_date)
        if action.type == "SPLIT_PO_SCHEDULE_LINE":
            part = action.parts[step.substep]
            record = self.sap.get(
                PO,
                LINE,
                {
                    "PurchasingDocument": action.po_number,
                    "PurchasingDocumentItem": action.po_item,
                    "ScheduleLine": str(int(action.schedule_line) + step.substep),
                },
            )
            return Decimal(
                str(record.data["ScheduleLineOrderQuantity"])
            ) == part.qty and record.data["ScheduleLineDeliveryDate"] == sap_date(
                part.delivery_date
            )
        return False

    def undo(self, step: Step, undo: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        action = step.action
        if action.type == "CREATE_STO":
            keys = {"PurchaseOrder": result["document"], "PurchaseOrderItem": "10"}
            record = self.sap.get(PO, ITEM, keys)
            documents = self.sap.query(
                "API_MATERIAL_DOCUMENT_SRV",
                "A_MaterialDocumentItem",
                filter=f"PurchaseOrder eq {odata_quote(str(result['document']))}",
            )
            if documents:
                raise ValueError("material movement exists; STO rollback refused")
            self.sap.update(
                PO, ITEM, keys, {"PurchasingDocumentDeletionCode": "L"}, etag=etag(record)
            )
            checked = self.sap.get(PO, ITEM, keys)
            if checked.data.get("PurchasingDocumentDeletionCode") != "L":
                raise ValueError("STO rollback verification failed")
            return {"sourceRef": checked.source_ref, "deleted": True}
        if action.type == "CHANGE_PO_DATE":
            current = self.sap.get(PO, LINE, undo["keys"])
            if current.data.get("ScheduleLineDeliveryDate") != sap_date(action.new_date):
                raise ValueError("schedule changed after execution; rollback refused")
            self.sap.update(
                PO,
                LINE,
                undo["keys"],
                {"ScheduleLineDeliveryDate": undo["value"]},
                etag=etag(current),
            )
            checked = self.sap.get(PO, LINE, undo["keys"])
            if checked.data.get("ScheduleLineDeliveryDate") != undo["value"]:
                raise ValueError("date rollback verification failed")
            return {"sourceRef": checked.source_ref, "restored": True}
        if action.type == "SPLIT_PO_SCHEDULE_LINE":
            if not self.verify(step, result):
                raise ValueError("split line changed; rollback refused")
            current = self.sap.get(PO, LINE, undo["keys"])
            if undo["type"] == "DELETE_LINE":
                self.sap.delete(PO, LINE, undo["keys"], etag=etag(current))
                try:
                    self.sap.get(PO, LINE, undo["keys"])
                except SapNotFoundError:
                    return {"sourceRef": current.source_ref, "deleted": True}
                raise ValueError("split deletion verification failed")
            self.sap.update(
                PO,
                LINE,
                undo["keys"],
                {
                    "ScheduleLineDeliveryDate": undo["value"],
                    "ScheduleLineOrderQuantity": undo["quantity"],
                },
                etag=etag(current),
            )
            checked = self.sap.get(PO, LINE, undo["keys"])
            if checked.data["ScheduleLineDeliveryDate"] != undo["value"] or Decimal(
                str(checked.data["ScheduleLineOrderQuantity"])
            ) != Decimal(str(undo["quantity"])):
                raise ValueError("split restoration verification failed")
            return {"sourceRef": checked.source_ref, "restored": True}
        raise ValueError("unsupported compensation")
