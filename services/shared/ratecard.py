"""Administered rate card (DR-11, BR-01): the only source of action costs and lead times.

Entries live in the config table as `RATE#{entryId}`; a cost always cites its entry as
`ratecard:{entryId}` (FR-IMP-03). Entries outside their validity window are ignored.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from services.shared.dynamo import from_item, table_name

CACHE_SECONDS = 60.0


@dataclass(frozen=True)
class Rate:
    entry_id: str
    action_type: str
    unit_cost_usd: Decimal
    fixed_cost_usd: Decimal
    lead_time_hours: Decimal
    from_plant: str | None = None
    to_plant: str | None = None
    supplier_id: str | None = None
    lane: str | None = None
    valid_from: str = "0001-01-01"
    valid_to: str = "9999-12-31"

    @property
    def source_ref(self) -> str:
        return f"ratecard:{self.entry_id}"

    def cost(self, qty: Decimal) -> Decimal:
        return self.fixed_cost_usd + self.unit_cost_usd * qty

    def valid_on(self, day: date) -> bool:
        return self.valid_from <= day.isoformat() <= self.valid_to


class RateCard:
    def __init__(
        self, client: Any, env: str | None = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._client = client
        self._table = table_name("config", env)
        self._clock = clock
        self._cached: tuple[float, list[Rate]] | None = None

    def entries(self) -> list[Rate]:
        if self._cached and self._clock() - self._cached[0] < CACHE_SECONDS:
            return self._cached[1]
        rates: list[Rate] = []
        arguments: dict[str, Any] = {
            "TableName": self._table,
            "FilterExpression": "begins_with(PK, :rate)",
            "ExpressionAttributeValues": {":rate": {"S": "RATE#"}},
        }
        while True:
            page = self._client.scan(**arguments)
            for item in page.get("Items", []):
                data = from_item(item)
                rates.append(
                    Rate(
                        entry_id=str(data["entryId"]),
                        action_type=str(data["actionType"]),
                        unit_cost_usd=Decimal(str(data["unitCostUsd"])),
                        fixed_cost_usd=Decimal(str(data["fixedCostUsd"])),
                        lead_time_hours=Decimal(str(data["leadTimeHours"])),
                        from_plant=data.get("fromPlant"),
                        to_plant=data.get("toPlant"),
                        supplier_id=data.get("supplierId"),
                        lane=data.get("lane"),
                        valid_from=str(data.get("validFrom", "0001-01-01")),
                        valid_to=str(data.get("validTo", "9999-12-31")),
                    )
                )
            if "LastEvaluatedKey" not in page:
                break
            arguments["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        rates.sort(key=lambda r: r.entry_id)
        self._cached = (self._clock(), rates)
        return rates

    def find(
        self,
        action_type: str,
        on: date,
        *,
        from_plant: str | None = None,
        to_plant: str | None = None,
        supplier_id: str | None = None,
    ) -> Rate | None:
        for rate in self.entries():
            if rate.action_type != action_type or not rate.valid_on(on):
                continue
            if from_plant is not None and rate.from_plant != from_plant:
                continue
            if to_plant is not None and rate.to_plant != to_plant:
                continue
            if supplier_id is not None and rate.supplier_id != supplier_id:
                continue
            return rate
        return None
