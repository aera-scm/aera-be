"""Projection inputs from SAP and from a plan's actions (FR-SIM-01, FR-SIM-03).

- On-hand stock and the consumption rate come from the stock and consumption services.
- Open purchase-order schedule lines of the material at the plant are receipts at their SAP
  date, except the case's own delayed order: it is received at the case's recovery date
  (the `recoveryAt` the impact used, cited to its signal) or, without one, not within the
  horizon.
- An option adds its receipts (transfer, air freight, alternate order) at the option's
  arrival, an issue at the donor for a transfer, and moves the case order's quantity as its
  split or date change says.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from services.optimizer.projection import Movement, PlantInputs, Projection, Requirement, project
from services.shared.dynamo import from_item, table_name
from services.shared.models import Case, Option
from services.shared.sap_values import at, number
from services.shared.triage import odata_quote
from services.tools.context import ToolContext, ToolError
from services.tools.sap_tools import (
    PO,
    component_requirements,
    sap_get_purchase_order,
    stock_position,
)


def _day(value: date) -> datetime:
    return datetime.combine(value, time(0, 0), tzinfo=UTC)


def plant_inputs(
    ctx: ToolContext, material: str, plant: str, *, skip_po: str | None = None
) -> PlantInputs:
    position = stock_position(ctx, material, plant)
    receipts: list[Movement] = []
    items = ctx.sap.query(
        PO,
        "A_PurchaseOrderItem",
        filter=f"Material eq {odata_quote(material)} and Plant eq {odata_quote(plant)}",
        run_id=ctx.run_id,
    )
    for item in items:
        order = str(item.data["PurchaseOrder"])
        if order == skip_po or item.data.get("IsCompletelyDelivered") is True:
            continue
        lines = ctx.sap.query(
            PO,
            "A_PurchaseOrderScheduleLine",
            filter=(
                f"PurchasingDocument eq {odata_quote(order)} and "
                f"PurchasingDocumentItem eq {odata_quote(str(item.data['PurchaseOrderItem']))}"
            ),
            run_id=ctx.run_id,
        )
        for line in lines:
            when = at(
                line.data.get("ScheduleLineDeliveryDate"), line.data.get("ScheduleLineDeliveryTime")
            )
            if when is not None:
                receipts.append(
                    Movement(
                        when,
                        number(line.data.get("ScheduleLineOrderQuantity")),
                        f"PO {order}",
                        line.source_ref,
                    )
                )
    requirements = tuple(
        Requirement(r["productionOrder"], r["requiredAt"], r["openQuantity"], r["sourceRef"])
        for r in component_requirements(ctx, material, plant)
    )
    refs = [*position["stockSourceRefs"]]
    if position["consumptionSourceRef"]:
        refs.append(position["consumptionSourceRef"])
    return PlantInputs(
        plant=plant,
        on_hand=position["unrestricted"],
        rate=position["consumptionPerHour"] or Decimal(0),
        movements=tuple(receipts),
        requirements=requirements,
        source_refs=(
            tuple(refs)
            + tuple(m.source_ref for m in receipts)
            + tuple(r.source_ref for r in requirements)
        ),
    )


def recovery(ctx: ToolContext, case_id: str) -> tuple[datetime, str] | None:
    """The recovery date the planner's impact analysis used (FR-IMP-02), with its source."""
    item = ctx.dynamodb.get_item(
        TableName=table_name("cases", ctx.env),
        Key={"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": "IMPACT"}},
        ConsistentRead=True,
    ).get("Item")
    figures = from_item(item).get("figures", []) if item else []
    found = next((f for f in figures if f.get("name") == "recoveryAt"), None)
    if found is None:
        return None
    moment = datetime.fromisoformat(str(found["value"]).replace("Z", "+00:00"))
    return moment, str(found["sourceRef"])


class CaseProjector:
    """Baseline and option projections for one case, per affected plant."""

    def __init__(self, ctx: ToolContext, case: Case) -> None:
        self.ctx = ctx
        self.case = case
        self.recovered = recovery(ctx, case.case_id)
        self.ordered = Decimal(0)
        self.po_ref = ""
        if case.po_number:
            try:
                po = sap_get_purchase_order(ctx, case.po_number)
            except ToolError:
                po = {"items": []}
            item = next((i for i in po["items"] if i.get("material") == case.material), None)
            if item is not None:
                self.ordered = Decimal(str(item["orderQuantity"]))
                self.po_ref = str(item["sourceRef"])
        self._plants: dict[str, PlantInputs] = {}

    def inputs(self, plant: str) -> PlantInputs:
        if plant not in self._plants:
            skip = self.case.po_number if plant == self.case.plant else None
            self._plants[plant] = plant_inputs(self.ctx, self.case.material, plant, skip_po=skip)
        return self._plants[plant]

    def _case_order(self, moments: list[tuple[datetime, Decimal]]) -> list[Movement]:
        return [
            Movement(moment, qty, f"PO {self.case.po_number}", self.po_ref)
            for moment, qty in moments
            if qty > 0
        ]

    def movements(self, options: list[Option]) -> dict[str, list[Movement]]:
        """What the chosen options add or move, per plant."""
        plant = self.case.plant
        moves: dict[str, list[Movement]] = {plant: []}
        order: list[tuple[datetime, Decimal]] = (
            [(self.recovered[0], self.ordered)] if self.recovered and self.ordered else []
        )
        for option in options:
            flown = Decimal(0)
            for action in option.actions:
                if action.type == "CREATE_STO":
                    moves[plant].append(
                        Movement(
                            option.arrival,
                            action.qty,
                            f"STO from {action.from_plant}",
                            option.cost_source_ref,
                        )
                    )
                    moves.setdefault(action.from_plant, []).append(
                        Movement(
                            self.ctx.now(), -action.qty, f"STO to {plant}", option.cost_source_ref
                        )
                    )
                elif action.type == "BOOK_AIR_FREIGHT":
                    flown += action.qty
                    moves[plant].append(
                        Movement(action.arrival, action.qty, "Air freight", option.cost_source_ref)
                    )
                elif action.type == "CREATE_PO_ALTERNATE":
                    moves[plant].append(
                        Movement(
                            option.arrival,
                            action.qty,
                            f"PO alternate {action.supplier_id}",
                            option.cost_source_ref,
                        )
                    )
                elif action.type == "CHANGE_PO_DATE":
                    order = [(_day(action.new_date), self.ordered)]
                elif action.type == "SPLIT_PO_SCHEDULE_LINE":
                    parts = (
                        action.parts[1:]
                        if flown or any(a.type == "BOOK_AIR_FREIGHT" for a in option.actions)
                        else action.parts
                    )
                    order = [(_day(p.delivery_date), p.qty) for p in parts]
                    flown = Decimal(0)  # the split already carries the remainder
            if flown and order:
                # The flown quantity leaves the order's later receipt.
                last, qty = order[-1]
                order[-1] = (last, max(qty - flown, Decimal(0)))
        moves[plant].extend(self._case_order(order))
        return moves

    def projections(self, options: list[Option]) -> dict[str, Projection]:
        now = self.ctx.now()
        result = {}
        for plant, moves in self.movements(options).items():
            base = self.inputs(plant)
            result[plant] = project(base.plus(*moves), now)
        return result


def projection_json(projections: dict[str, Projection]) -> dict[str, Any]:
    return {plant: p.json() for plant, p in sorted(projections.items())}
