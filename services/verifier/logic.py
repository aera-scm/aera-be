"""Deterministic M3 verification and confidence (V-01..V-13, BR-18)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import TypeAdapter, ValidationError

from services.shared.models import Action, CheckResult, Option, PlanRecord, ProposedPlan

ZERO = Decimal(0)
ONE = Decimal(1)
ACTION: TypeAdapter[Action] = TypeAdapter(Action)
REVERSIBLE = frozenset({"CREATE_STO", "CHANGE_PO_DATE", "SPLIT_PO_SCHEDULE_LINE"})
CHECK_IDS = frozenset(f"V-{index:02d}" for index in range(1, 14))
# V-10: how long an option's priced arrival stays valid before it must be re-priced (ADR-0029).
PRICE_MAX_AGE = timedelta(minutes=15)


def plan_hash(plan: ProposedPlan) -> str:
    def encode(value: Any) -> str:
        if isinstance(value, Decimal):
            return format(value.normalize(), "f")
        if hasattr(value, "isoformat"):
            return str(value.isoformat())
        raise TypeError(type(value).__name__)

    canonical = json.dumps(
        plan.model_dump(mode="python"),
        default=encode,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _moment(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if aware(value) else None
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if aware(moment) else None


def same(left: Any, right: Any) -> bool:
    """A figure equals its re-read value: the same number, or the same instant however the
    time was written (`2026-10-03T08:15:15Z` and `2026-10-03 08:15:15+00:00`)."""
    if left == right:
        return True
    if left is None or right is None:
        return False
    moments = (_moment(left), _moment(right))
    if moments[0] is not None and moments[1] is not None:
        return moments[0] == moments[1]
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except ArithmeticError:
        return False


def finite(value: Decimal, *, positive: bool = False) -> bool:
    return value.is_finite() and (value > 0 if positive else value >= 0)


def aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


@dataclass(frozen=True)
class Donor:
    on_hand: Decimal
    consumption_per_hour: Decimal
    customer_commitments: Decimal
    unreserved: Decimal
    # FR-SIM-04: when the donor's projection after this transfer runs dry, and the end of
    # the protected cover window; the projection must last at least that long.
    projected_stockout: datetime | None = None
    cover_until: datetime | None = None


@dataclass(frozen=True)
class OptionEvidence:
    read_at: datetime
    material: str
    plant: str
    expected_actions: tuple[dict[str, Any], ...]
    expected_cost: Decimal
    expected_coverage: Decimal
    expected_arrival: datetime
    need_at: datetime
    need_qty: Decimal
    rar_protected: Decimal
    lead_hours: Decimal
    reread: dict[tuple[str, str], Decimal | str] = field(default_factory=dict)
    plants: frozenset[str] = frozenset()
    suppliers: frozenset[str] = frozenset()
    compliance: dict[str, str] = field(default_factory=dict)
    donors: dict[tuple[str, str], Donor] = field(default_factory=dict)
    recipients: frozenset[str] = frozenset()
    allowed_recipients: frozenset[str] = frozenset()
    unconfirmed_fields: tuple[str, ...] = ()
    calendar_feasible: bool = False
    cancellation_fee: Decimal = ZERO
    # The clock the option was priced at (its `calc_option` draft); V-10 measures the lead
    # time from here. None means "priced now".
    priced_at: datetime | None = None


@dataclass(frozen=True)
class Corroboration:
    mrp_verified: bool = False
    agreeing_senders: frozenset[str] = frozenset()

    @property
    def score(self) -> Decimal:
        if self.mrp_verified or len(self.agreeing_senders) >= 2:
            return ONE
        return Decimal("0.7") if self.agreeing_senders else ZERO


@dataclass(frozen=True)
class Grounding:
    score: Decimal = ZERO
    relevance: Decimal = ZERO
    available: bool = False

    def valid(self) -> bool:
        return self.available and all(
            finite(value) and value <= 1 for value in (self.score, self.relevance)
        )


@dataclass(frozen=True)
class Verification:
    record: PlanRecord
    version_hash: str
    evidence: dict[str, OptionEvidence]
    corroboration: Corroboration
    grounding: Grounding

    def confidence_for(self, ids: tuple[str, ...]) -> Decimal:
        return confidence(self.record.plan, self.evidence, ids, self.corroboration, self.grounding)


def confidence(
    plan: ProposedPlan,
    evidence: dict[str, OptionEvidence],
    ids: tuple[str, ...],
    corroboration: Corroboration,
    grounding: Grounding,
) -> Decimal:
    figures = [
        (option, figure) for option in plan.options if option.id in ids for figure in option.figures
    ]
    supported = sum(
        1
        for option, figure in figures
        if figure.source_ref.startswith(("SAP:", "ratecard:", "planner:"))
        and option.id in evidence
        and same(evidence[option.id].reread.get((figure.source_ref, figure.name)), figure.value)
    )
    share = Decimal(supported) / len(figures) if figures else ZERO
    score = grounding.score if grounding.valid() else ZERO
    return Decimal("0.5") * share + Decimal("0.3") * corroboration.score + Decimal("0.2") * score


def is_reversible(option: Option, evidence: OptionEvidence) -> bool:
    return evidence.cancellation_fee == 0 and all(
        action.type in REVERSIBLE for action in option.actions
    )


def _quantities(option: Option) -> list[Decimal]:
    quantities = [option.coverage_units]
    for action in option.actions:
        if action.type == "SPLIT_PO_SCHEDULE_LINE":
            quantities.extend(part.qty for part in action.parts)
        elif hasattr(action, "qty"):
            quantities.append(action.qty)
    return quantities


def _transfers(options: list[Option]) -> dict[tuple[str, str], Decimal]:
    amounts: dict[tuple[str, str], Decimal] = {}
    for option in options:
        for action in option.actions:
            if action.type == "CREATE_STO":
                key = (action.material, action.from_plant)
                amounts[key] = amounts.get(key, ZERO) + action.qty
    return amounts


def _donor_ok(donor: Donor | None, quantity: Decimal, minimum_cover: Decimal) -> bool:
    if donor is None or not all(
        finite(value)
        for value in (
            donor.on_hand,
            donor.consumption_per_hour,
            donor.customer_commitments,
            donor.unreserved,
        )
    ):
        return False
    left = donor.on_hand - quantity
    projected = (
        donor.projected_stockout is None
        or donor.cover_until is None
        or donor.projected_stockout >= donor.cover_until
    )
    return (
        left >= donor.consumption_per_hour * 24 * minimum_cover
        and left >= donor.customer_commitments
        and projected
    )


def _check(
    check_id: str,
    passed: bool,
    detail: str,
    option_id: str | None,
    blocking: bool = True,
    reason: str = "",
) -> CheckResult:
    """A failed check names its reason after the rule (FR-VER-02)."""
    if not passed and reason:
        detail = f"{detail}: {reason}"
    return CheckResult(
        check_id=check_id, passed=passed, blocking=blocking, option_id=option_id, detail=detail
    )


def _donor_reason(donor: Donor | None, quantity: Decimal, minimum_cover: Decimal) -> str:
    if donor is None:
        return "donor stock could not be re-read"
    left = donor.on_hand - quantity
    floor = donor.consumption_per_hour * 24 * minimum_cover
    problems = []
    if left < floor:
        problems.append(f"{left} left after the transfer, minimum cover needs {floor}")
    if left < donor.customer_commitments:
        problems.append(f"{left} left, confirmed commitments need {donor.customer_commitments}")
    if (
        donor.projected_stockout is not None
        and donor.cover_until is not None
        and donor.projected_stockout < donor.cover_until
    ):
        problems.append(
            f"projected stock-out {donor.projected_stockout.isoformat()} before the cover "
            f"window ends {donor.cover_until.isoformat()}"
        )
    return "; ".join(problems) or "donor figures are not finite"


def verify(
    plan: ProposedPlan,
    evidence: dict[str, OptionEvidence],
    *,
    now: datetime,
    grounding: Grounding,
    corroboration: Corroboration,
    proposed_at: datetime,
    minimum_cover: Decimal = Decimal("2"),
    max_age: timedelta = timedelta(seconds=60),
    max_price_age: timedelta = PRICE_MAX_AGE,
) -> Verification:
    if not aware(now) or not finite(minimum_cover) or max_age <= timedelta(0):
        raise ValueError("invalid verifier clock or cover policy")
    checks: list[CheckResult] = []
    for option in plan.options:
        facts = evidence.get(option.id)
        fresh = (
            facts is not None
            and aware(facts.read_at)
            and timedelta(0) <= now - facts.read_at <= max_age
        )
        if not fresh or facts is None:
            checks.extend(
                _check(
                    check_id,
                    False,
                    "Trusted reread evidence unavailable or stale",
                    option.id,
                    check_id != "V-13",
                )
                for check_id in sorted(CHECK_IDS)
            )
            continue
        required = {"costUsd": option.cost_usd, "arrival": str(option.arrival)}
        figure_values = {figure.name: figure.value for figure in option.figures}
        source_ok = (
            bool(option.figures)
            and all(
                same(facts.reread.get((figure.source_ref, figure.name)), figure.value)
                for figure in option.figures
            )
            and all(same(figure_values.get(name), value) for name, value in required.items())
        )
        source_ok = source_ok and any(
            figure.name == "costUsd" and figure.source_ref == option.cost_source_ref
            for figure in option.figures
        )
        source_ok = source_ok and (
            option.cost_usd == facts.expected_cost
            and option.coverage_units == facts.expected_coverage
            and option.arrival == facts.expected_arrival
        )
        problems = [] if option.figures else ["option has no figures"]
        problems += [
            f"{figure.name} {figure.value} differs from re-read "
            f"{facts.reread.get((figure.source_ref, figure.name), 'nothing')}"
            for figure in option.figures
            if not same(facts.reread.get((figure.source_ref, figure.name)), figure.value)
        ]
        problems += [
            f"figure {name} missing or not equal to the option"
            for name, value in required.items()
            if not same(figure_values.get(name), value)
        ]
        problems += [
            f"{name} {given} differs from recalculated {expected}"
            for name, given, expected in (
                ("costUsd", option.cost_usd, facts.expected_cost),
                ("coverageUnits", option.coverage_units, facts.expected_coverage),
                ("arrival", option.arrival, facts.expected_arrival),
            )
            if given != expected
        ]
        checks.append(
            _check(
                "V-01",
                source_ok,
                "Figures and recalculated values match fresh sources",
                option.id,
                reason="; ".join(problems) or "cost figure does not cite the cost source",
            )
        )
        quantities = _quantities(option)
        valid_qty = all(
            finite(value, positive=True) and value == value.to_integral_value()
            for value in quantities
        )
        flag = (
            "Partial coverage"
            if option.coverage_units < facts.need_qty
            else (
                "Over-coverage above 25%; review required"
                if option.coverage_units > facts.need_qty * Decimal("1.25")
                else "Quantity within need"
            )
        )
        checks.append(
            _check(
                "V-02", valid_qty, flag, option.id, reason="quantities must be positive integers"
            )
        )
        future = aware(option.arrival) and option.arrival > now
        dates = [
            action.delivery_date for action in option.actions if hasattr(action, "delivery_date")
        ]
        dates += [action.new_date for action in option.actions if action.type == "CHANGE_PO_DATE"]
        dates += [
            part.delivery_date
            for action in option.actions
            if action.type == "SPLIT_PO_SCHEDULE_LINE"
            for part in action.parts
        ]
        future = future and all(day >= now.date() for day in dates)
        late = (
            not aware(facts.need_at) or option.arrival > facts.need_at
            if aware(option.arrival)
            else True
        )
        checks.append(
            _check(
                "V-03",
                future and aware(facts.need_at),
                "Late arrival; partial coverage requires review" if late else "Arrival before need",
                option.id,
                reason="arrival or a delivery date is not in the future"
                if aware(facts.need_at)
                else "need date unknown",
            )
        )
        try:
            expected = [ACTION.validate_python(action) for action in facts.expected_actions]
            valid_actions = [action.model_dump() for action in option.actions] == [
                action.model_dump() for action in expected
            ]
            valid_actions = valid_actions and all(
                all(value != "" for value in action.model_dump().values())
                for action in option.actions
            )
        except (ValidationError, ValueError):
            valid_actions = False
            expected = []
        given = [action.model_dump(mode="json") for action in option.actions]
        recalculated = [action.model_dump(mode="json") for action in expected]
        differences = [
            f"{key} {one.get(key)} differs from recalculated {other.get(key)}"
            for one, other in zip(given, recalculated, strict=False)
            for key in sorted(set(one) | set(other))
            if one.get(key) != other.get(key)
        ]
        if len(given) != len(recalculated):
            differences.append(f"{len(given)} actions, recalculation has {len(recalculated)}")
        checks.append(
            _check(
                "V-04",
                valid_actions and bool(expected if valid_actions else []),
                "Allowlisted action parameters match independent calculation",
                option.id,
                reason="; ".join(differences[:5]) or "action parameters incomplete or invalid",
            )
        )
        masters = facts.plant in facts.plants
        for action in option.actions:
            if hasattr(action, "supplier_id"):
                masters = masters and action.supplier_id in facts.suppliers
            if action.type == "CREATE_STO":
                masters = (
                    masters and action.from_plant in facts.plants and action.to_plant == facts.plant
                )
                masters = masters and action.material == facts.material
            if action.type == "CREATE_PO_ALTERNATE":
                masters = (
                    masters and action.plant == facts.plant and action.material == facts.material
                )
        checks.append(
            _check(
                "V-05",
                masters,
                "Supplier and plant master data verified",
                option.id,
                reason="a supplier, plant or material is missing, blocked or not the case's",
            )
        )
        compliance = all(
            facts.compliance.get(action.supplier_id) == "APPROVED"
            for action in option.actions
            if action.type == "CREATE_PO_ALTERNATE"
        )
        statuses = ", ".join(
            f"{action.supplier_id} is {facts.compliance.get(action.supplier_id) or 'unknown'}"
            for action in option.actions
            if action.type == "CREATE_PO_ALTERNATE"
        )
        checks.append(
            _check(
                "V-06",
                compliance,
                "Alternate supplier must be APPROVED",
                option.id,
                reason=statuses,
            )
        )
        transfers = _transfers([option])
        checks.append(
            _check(
                "V-07",
                all(
                    _donor_ok(facts.donors.get(key), qty, minimum_cover)
                    for key, qty in transfers.items()
                ),
                "Donor cover and confirmed customer orders protected",
                option.id,
                reason="; ".join(
                    f"donor {plant}: "
                    + _donor_reason(facts.donors.get((material, plant)), qty, minimum_cover)
                    for (material, plant), qty in transfers.items()
                    if not _donor_ok(facts.donors.get((material, plant)), qty, minimum_cover)
                ),
            )
        )
        checks.append(
            _check(
                "V-08",
                all(
                    key in facts.donors and facts.donors[key].unreserved >= qty
                    for key, qty in transfers.items()
                ),
                "Ledger dry run has sufficient unreserved stock",
                option.id,
                reason="; ".join(
                    f"donor {plant} has "
                    f"{facts.donors[(m, plant)].unreserved if (m, plant) in facts.donors else 0}"
                    f" unreserved, transfer needs {qty}"
                    for (m, plant), qty in transfers.items()
                ),
            )
        )
        checks.append(
            _check(
                "V-09",
                finite(option.cost_usd)
                and finite(facts.rar_protected, positive=True)
                and option.cost_usd < facts.rar_protected,
                "Cost must be lower than protected revenue",
                option.id,
                reason=f"cost {option.cost_usd} USD, protected revenue {facts.rar_protected} USD",
            )
        )
        # ADR-0029: the arrival was computed when the option was priced, so the lead time is
        # measured from that moment; a price older than PRICE_MAX_AGE is no longer feasible.
        priced = facts.priced_at or now
        feasible = (
            finite(facts.lead_hours)
            and facts.calendar_feasible
            and aware(option.arrival)
            and aware(priced)
            and timedelta(0) <= now - priced <= max_price_age
        )
        feasible = feasible and option.arrival >= priced + timedelta(hours=float(facts.lead_hours))
        lead = facts.lead_hours if finite(facts.lead_hours) else ZERO
        earliest = priced + timedelta(hours=float(lead))
        checks.append(
            _check(
                "V-10",
                feasible,
                "Lead time and calendar feasible",
                option.id,
                reason=f"priced {priced.isoformat()}, verified {now.isoformat()} "
                f"(at most {int(max_price_age.total_seconds() // 60)} min old); arrival "
                f"{option.arrival.isoformat()}, earliest with {facts.lead_hours} h lead "
                f"{earliest.isoformat()}",
            )
        )
        checks.append(
            _check(
                "V-11",
                facts.recipients <= facts.allowed_recipients,
                "Recipients must belong to case master-data allowlist",
                option.id,
                reason=f"{len(facts.recipients - facts.allowed_recipients)} address(es) "
                "outside master data",
            )
        )
        checks.append(
            _check(
                "V-12",
                not facts.unconfirmed_fields,
                "No UNCONFIRMED critical field may be used",
                option.id,
                reason="unconfirmed: " + ", ".join(facts.unconfirmed_fields),
            )
        )
        checks.append(
            _check(
                "V-13",
                grounding.valid()
                and grounding.score >= Decimal("0.7")
                and grounding.relevance >= Decimal("0.7"),
                "Grounding and relevance must each reach 0.7",
                option.id,
                False,
            )
        )
    selected = [option for option in plan.options if option.id in plan.chosen]
    totals = (
        len(plan.chosen) == len(set(plan.chosen))
        and plan.total_cost_usd == sum((option.cost_usd for option in selected), ZERO)
        and plan.coverage_units == sum((option.coverage_units for option in selected), ZERO)
    )
    checks.append(_check("V-01", totals, "Chosen IDs and plan totals must match options", None))
    chosen_facts = [evidence[option.id] for option in selected if option.id in evidence]
    protected = min((facts.rar_protected for facts in chosen_facts), default=ZERO)
    checks.append(
        _check(
            "V-09",
            finite(plan.total_cost_usd) and plan.total_cost_usd < protected,
            "Combined plan cost must be lower than protected revenue",
            None,
        )
    )
    for key, qty in _transfers(selected).items():
        donors = [facts.donors[key] for facts in chosen_facts if key in facts.donors]
        checks.append(
            _check(
                "V-07",
                bool(donors) and all(_donor_ok(donor, qty, minimum_cover) for donor in donors),
                "Combined transfers preserve donor cover",
                None,
            )
        )
        checks.append(
            _check(
                "V-08",
                bool(donors) and all(donor.unreserved >= qty for donor in donors),
                "Combined transfers fit unreserved quantity",
                None,
            )
        )
    record = PlanRecord(
        plan=plan.model_copy(deep=True),
        checks=checks,
        proposed_at=proposed_at,
        verified_at=now,
        confidence=confidence(plan, evidence, tuple(plan.chosen), corroboration, grounding),
    )
    return Verification(record, plan_hash(plan), evidence, corroboration, grounding)
