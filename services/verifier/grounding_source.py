"""The grounding source for FR-VER-03: the retrieved SAP and case facts as English sentences.

The contextual grounding check compares the plan rationale with this text. Measured on the
live reference plan (ADR-0032), the same facts as JSON scored GROUNDING 0.06 and as sentences
0.23, because the rationale speaks in sentences, times and totals. Every sentence here comes
from a fresh read the Verifier already made (SAP, rate card, ledger, accepted signals); none
is taken from the agent's text.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from services.optimizer.inputs import CaseProjector
from services.shared.models import Case, ProposedPlan, SignalStatus
from services.tools.context import ToolContext, ToolError
from services.tools.reliability import profile_for
from services.tools.sap_tools import find_sources, sap_get_purchase_order
from services.verifier.logic import OptionEvidence


def _time(value: Any) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        return str(value)
    return f"{value:%Y-%m-%dT%H:%M} UTC ({value:%H:%M} on {value:%b} {value.day})"


def _number(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return str(value)
    if not number.is_finite():
        return str(value)
    return f"{number:,.0f}" if number == number.to_integral_value() else f"{number:,.2f}"


def render(
    ctx: ToolContext,
    case: Case,
    plan: ProposedPlan,
    impact: dict[str, Any],
    evidence: dict[str, OptionEvidence],
    need_at: datetime,
    projector: CaseProjector,
    supplier: Callable[[str], dict[str, Any] | None],
) -> str:
    lines = [f"Case {case.case_id}: material {case.material} at plant {case.plant}."]
    if case.po_number:
        try:
            po = sap_get_purchase_order(ctx, case.po_number)
        except ToolError:
            po = None
        if po is not None:
            lines.append(f"Purchase order {case.po_number} is with supplier {po.get('supplier')}.")
            for item in po.get("items", []):
                lines.append(
                    f"Purchase order {case.po_number} item {item.get('item')}: material "
                    f"{item.get('material')}, {_number(item.get('orderQuantity'))} PC ordered."
                )
    lines += [
        f"On hand {_number(impact['onHand'])} PC; consumption "
        f"{_number(impact['consumptionPerHour'])} PC per hour; stock-out at {_time(need_at)}, "
        f"{impact['hoursToStockout']} hours away.",
        f"Revenue at risk USD {_number(impact['rarUsd'] or 0)} across "
        f"{len(impact['salesOrdersAtRisk'])} sales orders; {_number(impact['unitsAtRisk'])} "
        "units short.",
    ]
    for order in impact["productionOrdersAtRisk"]:
        lines.append(
            f"Production order {order['productionOrder']} needs "
            f"{_number(order['openQuantity'])} PC at {_time(order['requiredAt'])}; "
            f"{_number(order['unitsShort'])} PC short."
        )
    for sale in impact["salesOrdersAtRisk"]:
        lines.append(
            f"Sales order {sale['salesOrder']} item {sale['item']} worth USD "
            f"{_number(sale['netUsd'])}, confirmed for {sale['confirmedDate']}."
        )
    suppliers: set[str] = set()
    for option in plan.options:
        facts = evidence.get(option.id)
        if facts is None:
            continue
        actions = ", ".join(action.type for action in option.actions)
        lines.append(
            f"Option {option.id} ({actions}): {_number(facts.expected_coverage)} PC, cost USD "
            f"{_number(facts.expected_cost)}, arrives {_time(facts.expected_arrival)}, lead time "
            f"{_number(facts.lead_hours)} hours."
        )
        for (_, name), value in sorted(facts.reread.items(), key=lambda item: item[0][1]):
            if name in ("costUsd", "arrival"):
                continue
            shown = _time(value) if name.endswith("At") else _number(value)
            lines.append(f"Option {option.id} {name}: {shown}.")
        for action in option.actions:
            supplier_id = getattr(action, "supplier_id", None)
            if supplier_id:
                suppliers.add(supplier_id)
            if action.type == "SPLIT_PO_SCHEDULE_LINE":
                for part in action.parts:
                    lines.append(
                        f"Option {option.id} splits the schedule line: {_number(part.qty)} PC "
                        f"delivered {part.delivery_date}."
                    )
        for (material, plant), donor in sorted(facts.donors.items()):
            lines.append(
                f"Donor plant {plant} holds {_number(donor.on_hand)} PC of {material}, "
                f"consumes {_number(donor.consumption_per_hour)} PC per hour, and has "
                f"{_number(donor.unreserved)} PC unreserved."
            )
    by_id = {option.id: option for option in plan.options}
    chosen = [by_id[oid] for oid in plan.chosen if oid in by_id]
    known = [evidence[o.id] for o in chosen if o.id in evidence]
    if known:
        lines.append(
            f"The chosen options {' + '.join(o.id for o in chosen)} together cost USD "
            f"{_number(sum((f.expected_cost for f in known), Decimal(0)))} and cover "
            f"{_number(sum((f.expected_coverage for f in known), Decimal(0)))} PC."
        )
        for plant, projection in sorted(projector.projections(chosen).items()):
            moment = projection.first_stockout
            lines.append(
                f"With the chosen options, plant {plant} first runs out at {_time(moment)}."
                if moment
                else f"With the chosen options, plant {plant} does not run out in the horizon."
            )
    for signal in ctx.signals.for_case(case.case_id):
        if signal.status is not SignalStatus.ACCEPTED:
            continue
        for field in signal.fields:
            lines.append(
                f"A verified signal from {signal.sender_id} states {field.name} "
                f"{field.value} ({field.status.value})."
            )
    try:
        sources = find_sources(
            ctx,
            case.material,
            case.plant,
            impact["unitsAtRisk"] or 1,
            (ctx.now() + timedelta(days=7)).isoformat(),
        )
    except ToolError:
        sources = {}
    for transfer in sources.get("transfers", []):
        lines.append(
            f"Plant {transfer['fromPlant']} has {_number(transfer['freeQuantity'])} PC free "
            f"above its minimum cover of {_number(transfer['minimumCover'])} PC."
        )
    for alternate in sources.get("alternateSuppliers", []):
        suppliers.add(str(alternate.get("supplierId")))
    for supplier_id in sorted(suppliers):
        record = supplier(supplier_id) or {}
        history = (
            "has recent SAP delivery history"
            if profile_for(ctx, supplier_id, case.material)
            else "has no recent SAP delivery history"
        )
        lines.append(
            f"Supplier {supplier_id} ({record.get('SupplierName') or 'name not in master data'}) "
            f"has compliance status {record.get('ComplianceStatus') or 'unknown'} and {history}."
        )
    return "\n".join(lines)
