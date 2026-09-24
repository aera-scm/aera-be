"""Trusted re-reads for the Verifier (FR-VER-01, FR-VER-02, BR-18).

Every fact comes from SAP, the rate card, the ledger or case records, re-read now; nothing is
taken from what the agent wrote. Each option is recalculated from the parameters recorded
with its `calc_option` draft, at the draft's own clock, so a figure that changed in SAP since
the proposal shows up as a mismatch (V-01, V-04). An option that cannot be recalculated gets
no evidence, and every check on it fails closed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from services.execution.ledger import Ledger
from services.notifier.handler import INTERNAL_ROLES, partner_emails
from services.rules.br_02 import usable
from services.shared.dynamo import from_item, table_name
from services.shared.models import Case, Option, ProposedPlan, SignalStatus
from services.shared.sap_client import SapNotFoundError
from services.shared.triage import STOCK, odata_quote
from services.tools.calc import compute_impact, compute_option, draft_key
from services.tools.context import ToolContext, ToolError
from services.tools.sap_tools import (
    PARTNER,
    PO,
    component_requirements,
    stock_position,
)
from services.verifier.logic import Corroboration, Donor, OptionEvidence

MRP = "ZAERA_MIRROR_SRV"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class Facts:
    evidence: dict[str, OptionEvidence]
    corroboration: Corroboration
    stockout: datetime
    source: str  # the grounding source for FR-VER-03


def _number(value: Any) -> Decimal | str:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    return str(value)


def _at(ctx: ToolContext, moment: datetime) -> ToolContext:
    return ToolContext(
        sap=ctx.sap,
        dynamodb=ctx.dynamodb,
        bus=ctx.bus,
        clock=lambda: moment,
        env=ctx.env,
        actor="verifier",
    )


class EvidenceReader:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self.ledger = Ledger(ctx.dynamodb, ctx.env)
        self._cases = table_name("cases", ctx.env)

    def _item(self, case_id: str, sk: str) -> dict[str, Any] | None:
        item = self.ctx.dynamodb.get_item(
            TableName=self._cases,
            Key={"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": sk}},
            ConsistentRead=True,
        ).get("Item")
        return from_item(item) if item else None

    # Case-wide facts ---------------------------------------------------------------------

    def impact(self, case: Case) -> dict[str, Any]:
        """Fresh impact, with the recovery date the planner's analysis used (if any)."""
        recorded = self._item(case.case_id, "IMPACT") or {}
        recovery = next(
            (f for f in recorded.get("figures", []) if f.get("name") == "recoveryAt"), None
        )
        return compute_impact(
            self.ctx,
            case.case_id,
            str(recovery["value"]) if recovery else None,
            str(recovery["sourceRef"]) if recovery else None,
        )

    def corroboration(self, case: Case) -> Corroboration:
        rows = self.ctx.sap.query(
            MRP,
            "MRPExceptionMessage",
            filter=(
                f"Material eq {odata_quote(case.material)} and Plant eq {odata_quote(case.plant)}"
            ),
        )
        senders = frozenset(
            signal.sender_id
            for signal in self.ctx.signals.for_case(case.case_id)
            if signal.status is SignalStatus.ACCEPTED and signal.sender_verified
        )
        return Corroboration(mrp_verified=bool(rows), agreeing_senders=senders)

    def plants(self, material: str) -> frozenset[str]:
        rows = self.ctx.sap.query(
            STOCK, "A_MatlStkInAcctMod", filter=f"Material eq {odata_quote(material)}"
        )
        return frozenset(str(r.data["Plant"]) for r in rows)

    def supplier(self, supplier_id: str) -> dict[str, Any] | None:
        try:
            record = self.ctx.sap.get(PARTNER, "A_Supplier", {"Supplier": supplier_id})
        except SapNotFoundError:
            return None
        return dict(record.data)

    def case_supplier(self, case: Case) -> str | None:
        if not case.po_number:
            return None
        try:
            header = self.ctx.sap.get(PO, "A_PurchaseOrder", {"PurchaseOrder": case.po_number})
        except SapNotFoundError:
            return None
        return str(header.data.get("Supplier") or "") or None

    def allowed_recipients(self, case: Case, plan: ProposedPlan) -> frozenset[str]:
        """BR-03: master-data addresses of the parties on the case."""
        partners = set(INTERNAL_ROLES.values())
        supplier = self.case_supplier(case)
        if supplier:
            partners.add(supplier)
        for option in plan.options:
            for action in option.actions:
                if hasattr(action, "supplier_id"):
                    partners.add(action.supplier_id)
        emails: set[str] = set()
        for partner in sorted(partners):
            emails |= partner_emails(self.ctx.sap, partner)
        return frozenset(emails)

    def donor(self, material: str, plant: str, cover_days: Decimal) -> Donor:
        position = stock_position(self.ctx, material, plant)
        per_hour = position["consumptionPerHour"] or Decimal(0)
        horizon = self.ctx.now() + timedelta(days=float(cover_days))
        # Confirmed production demand at the donor inside the protected cover window.
        committed = sum(
            (
                r["openQuantity"]
                for r in component_requirements(self.ctx, material, plant)
                if r["requiredAt"] <= horizon
            ),
            Decimal(0),
        )
        on_hand = position["unrestricted"]
        held = self.ledger.allocated(material=material, plant=plant)
        return Donor(
            on_hand=on_hand,
            consumption_per_hour=per_hour,
            customer_commitments=committed,
            unreserved=on_hand - held,
        )

    # Per option ------------------------------------------------------------------------

    def unconfirmed(self, case: Case, option: Option) -> tuple[str, ...]:
        fields = {
            (field.signal_id, str(field.name)): field
            for signal in self.ctx.signals.for_case(case.case_id)
            for field in signal.fields
        }
        found = []
        for figure in option.figures:
            if not figure.source_ref.startswith("signal:"):
                continue
            signal_id, _, name = figure.source_ref.removeprefix("signal:").partition("/")
            field = fields.get((signal_id, name))
            if field is None or not usable(field):
                found.append(figure.name)
        return tuple(found)

    def option(
        self,
        case: Case,
        plan: ProposedPlan,
        option: Option,
        *,
        need_at: datetime,
        impact: dict[str, Any],
        plants: frozenset[str],
        allowed: frozenset[str],
        cover_days: Decimal,
    ) -> OptionEvidence | None:
        draft = self._item(
            case.case_id,
            "DRAFT#" + draft_key(option.cost_usd, option.coverage_units, option.cost_source_ref),
        )
        if draft is None or "params" not in draft:
            return None
        try:
            fresh, rate = compute_option(
                _at(self.ctx, datetime.fromisoformat(str(draft["createdAt"]))),
                case.case_id,
                str(draft["actionType"]),
                dict(draft["params"]),
            )
        except ToolError:
            return None
        suppliers: set[str] = set()
        compliance: dict[str, str] = {}
        for action in option.actions:
            supplier_id = getattr(action, "supplier_id", None)
            if supplier_id is None:
                continue
            record = self.supplier(supplier_id)
            if record is None:
                continue
            compliance[supplier_id] = str(record.get("ComplianceStatus") or "")
            if not record.get("PurchasingIsBlocked"):
                suppliers.add(supplier_id)
        donors = {
            (action.material, action.from_plant): self.donor(
                action.material, action.from_plant, cover_days
            )
            for action in option.actions
            if action.type == "CREATE_STO"
        }
        text = " ".join([plan.rationale, option.name, option.rationale])
        text += " " + " ".join(str(f.value) for f in option.figures)
        return OptionEvidence(
            read_at=self.ctx.now(),
            material=case.material,
            plant=case.plant,
            expected_actions=tuple(fresh["actions"]),
            expected_cost=fresh["costUsd"],
            expected_coverage=fresh["coverageUnits"],
            expected_arrival=fresh["arrival"],
            need_at=need_at,
            need_qty=Decimal(str(impact["unitsAtRisk"])),
            rar_protected=Decimal(str(impact["rarUsd"] or 0)),
            lead_hours=rate.lead_time_hours,
            reread={(f["sourceRef"], f["name"]): _number(f["value"]) for f in fresh["figures"]},
            plants=plants,
            suppliers=frozenset(suppliers),
            compliance=compliance,
            donors=donors,
            recipients=frozenset(e.lower() for e in EMAIL.findall(text)),
            allowed_recipients=allowed,
            unconfirmed_fields=self.unconfirmed(case, option),
            # The rate card's lead time is the lane's calendar; a lane without a valid entry
            # cannot be recalculated and has no evidence at all.
            calendar_feasible=True,
        )

    def gather(self, case: Case, plan: ProposedPlan) -> Facts:
        impact = self.impact(case)
        now = self.ctx.now()
        need_at = impact["stockoutAt"] or now
        plants = self.plants(case.material)
        allowed = self.allowed_recipients(case, plan)
        cover = self.ctx.config.decimal("DONOR_MIN_COVER_DAYS")
        evidence: dict[str, OptionEvidence] = {}
        for option in plan.options:
            facts = self.option(
                case,
                plan,
                option,
                need_at=need_at,
                impact=impact,
                plants=plants,
                allowed=allowed,
                cover_days=cover,
            )
            if facts is not None:
                evidence[option.id] = facts
        source = json.dumps(
            {
                "case": case.case_id,
                "material": case.material,
                "plant": case.plant,
                "impact": {f["name"]: f["value"] for f in impact["figures"]},
                "stockoutAt": str(need_at),
                "unitsAtRisk": str(impact["unitsAtRisk"]),
                "revenueAtRiskUsd": str(impact["rarUsd"]),
                "options": {
                    oid: {
                        "costUsd": str(facts.expected_cost),
                        "coverageUnits": str(facts.expected_coverage),
                        "arrival": str(facts.expected_arrival),
                        "actions": [a.get("type") for a in facts.expected_actions],
                        "leadHours": str(facts.lead_hours),
                    }
                    for oid, facts in evidence.items()
                },
            },
            default=str,
        )
        return Facts(evidence, self.corroboration(case), need_at, source)
