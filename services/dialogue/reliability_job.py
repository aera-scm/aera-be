"""FR-LRN-01/03: refresh SAP-only supplier statistics in the analytics table."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.dialogue.history import PO, load_history
from services.dialogue.reliability import compute
from services.shared.dynamo import from_item, table_name, to_item
from services.shared.sap_client import SapClient
from services.shared.sap_values import results


def key(supplier_id: str, material: str) -> dict[str, Any]:
    return {
        "PK": {"S": f"SUPPLIER#{supplier_id}"},
        "SK": {"S": f"MATERIAL#{material}"},
    }


@dataclass
class ReliabilityJob:
    sap: SapClient
    dynamodb: Any
    env: str | None = None

    def refresh(self, now: datetime | None = None) -> int:
        observed = now or datetime.now(UTC)
        if observed.tzinfo is None:
            raise ValueError("reliability observation time must be aware")
        orders = self.sap.query(PO, "A_PurchaseOrder", expand="to_PurchaseOrderItem")
        pairs = {
            (str(order.data["Supplier"]), str(item["Material"]))
            for order in orders
            for item in results(order.data.get("to_PurchaseOrderItem"))
            if order.data.get("Supplier") and item.get("Material")
        }
        count = 0
        for supplier_id, material in sorted(pairs):
            history = load_history(self.sap, supplier_id, material, observed)
            if not history:
                continue
            profile = compute(supplier_id, material, history, observed)
            self.dynamodb.put_item(
                TableName=table_name("analytics", self.env),
                Item=to_item(
                    {
                        "PK": f"SUPPLIER#{supplier_id}",
                        "SK": f"MATERIAL#{material}",
                        "supplierId": supplier_id,
                        "material": material,
                        "windowDays": profile.window_days,
                        "sampleSize": profile.sample_size,
                        "onTimeRate": profile.on_time_rate,
                        "meanDelayDays": profile.mean_delay_days,
                        "p90DelayDays": profile.p90_delay_days,
                        "partialRate": profile.partial_rate,
                        "computedAt": profile.computed_at,
                        "sourceRefs": profile.source_refs,
                    }
                ),
            )
            count += 1
        return count


def read_profile(
    dynamodb: Any, supplier_id: str, material: str, env: str | None
) -> dict[str, Any] | None:
    item = dynamodb.get_item(
        TableName=table_name("analytics", env),
        Key=key(supplier_id, material),
        ConsistentRead=True,
    ).get("Item")
    if item is None:
        return None
    profile = from_item(item, keep_decimals=False)
    profile.pop("PK", None)
    profile.pop("SK", None)
    return profile
