"""BR-05/17/22/23: tier selection, independent urgent parts and deadlines."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from itertools import combinations

from services.shared.models import ApproverLimit
from services.verifier.logic import CHECK_IDS, Verification, aware, finite, is_reversible, plan_hash


@dataclass(frozen=True)
class Policy:
    auto_limit: Decimal = Decimal("25000")
    confidence_min: Decimal = Decimal("0.85")
    audit_share: Decimal = Decimal("0.2")
    approval_window: timedelta = timedelta(hours=4)
    kill_switch: bool = False
    limit_hit: bool = False


@dataclass(frozen=True)
class Part:
    id: str
    options: tuple[str, ...]
    tier: int
    cost: Decimal
    confidence: Decimal
    sampled: bool
    approver: str | None = None
    backup: str | None = None
    deadline: datetime | None = None
    reminder: datetime | None = None


@dataclass(frozen=True)
class Route:
    tier: int
    version_hash: str
    parts: tuple[Part, ...] = ()
    reason: str | None = None


DEFAULT_POLICY = Policy()


def eligible(
    limits: list[ApproverLimit], plant: str, cost: Decimal, now: datetime
) -> list[ApproverLimit]:
    return sorted(
        (
            limit
            for limit in limits
            if limit.plant == plant
            and limit.valid_from <= now.date() <= limit.valid_to
            and finite(limit.limit_usd)
            and limit.limit_usd >= cost
        ),
        key=lambda limit: (limit.limit_usd, limit.user_id),
    )


def route(
    verified: Verification,
    *,
    now: datetime,
    stockout: datetime,
    plant: str,
    limits: list[ApproverLimit],
    policy: Policy = DEFAULT_POLICY,
) -> Route:
    plan = verified.record.plan
    digest = plan_hash(plan)

    def blocked(reason: str) -> Route:
        return Route(3, digest, reason=reason)

    if not aware(now) or not aware(stockout):
        raise ValueError("routing requires timezone-aware clocks")
    if (
        not finite(policy.auto_limit)
        or not all(finite(v) and v <= 1 for v in (policy.confidence_min, policy.audit_share))
        or policy.approval_window <= timedelta(0)
    ):
        raise ValueError("invalid routing policy")
    if policy.kill_switch:
        return blocked("KILL_SWITCH: advise-only")
    if policy.limit_hit:
        return blocked("RUN_LIMIT")
    if digest != verified.version_hash:
        return blocked("STALE_VERIFICATION")
    if verified.record.verified_at is None or not timedelta(
        0
    ) <= now - verified.record.verified_at <= timedelta(seconds=60):
        return blocked("STALE_VERIFICATION")
    chosen = tuple(plan.chosen)
    if len(chosen) != len(set(chosen)) or not verified.grounding.valid():
        return blocked("INCOMPLETE_VERIFICATION")
    for oid in chosen:
        checks = [c for c in verified.record.checks if c.option_id == oid]
        if {c.check_id for c in checks} != CHECK_IDS or oid not in verified.evidence:
            return blocked("INCOMPLETE_VERIFICATION")
    checks = [c for c in verified.record.checks if c.option_id is None or c.option_id in chosen]
    if not {"V-01", "V-09"} <= {c.check_id for c in checks if c.option_id is None}:
        return blocked("INCOMPLETE_VERIFICATION")
    if any(c.blocking and not c.passed for c in checks):
        return blocked("BLOCKING_CHECK")
    if verified.confidence_for(chosen) < policy.confidence_min:
        return blocked("LOW_CONFIDENCE")
    options = {o.id: o for o in plan.options}

    def auto(ids: tuple[str, ...]) -> bool:
        return (
            bool(ids)
            and sum((options[i].cost_usd for i in ids), Decimal(0)) <= policy.auto_limit
            and all(is_reversible(options[i], verified.evidence[i]) for i in ids)
            and verified.confidence_for(ids) >= policy.confidence_min
            and all(c.passed for c in checks if c.option_id is None or c.option_id in ids)
        )

    def part(ids: tuple[str, ...], tier: int) -> Part:
        cost = sum((options[i].cost_usd for i in ids), Decimal(0))
        part_id = sha256(f"{digest}|{'|'.join(sorted(ids))}".encode()).hexdigest()
        sampled = tier == 1 and Decimal(int(part_id[:16], 16)) / Decimal(2**64) < policy.audit_share
        if tier == 1:
            return Part(part_id, ids, tier, cost, verified.confidence_for(ids), sampled)
        people = eligible(limits, plant, cost, now)
        lead = max(verified.evidence[i].lead_hours for i in ids)
        deadline = min(stockout - timedelta(hours=float(lead)), now + policy.approval_window)
        return Part(
            part_id,
            ids,
            tier,
            cost,
            verified.confidence_for(ids),
            False,
            people[0].user_id if people else None,
            next((p.user_id for p in people if p.user_id != people[0].user_id), None)
            if people
            else None,
            deadline,
            now + (deadline - now) / 2,
        )

    if auto(chosen):
        return Route(1, digest, (part(chosen, 1),))
    immediate: tuple[str, ...] = ()
    fastest = min(verified.evidence[i].lead_hours for i in chosen)
    if stockout - now < policy.approval_window + timedelta(hours=float(fastest)):
        candidates = [
            subset
            for size in range(1, len(chosen))
            for subset in combinations(chosen, size)
            if auto(subset)
        ]
        if candidates:
            immediate = max(
                candidates, key=lambda ids: (sum(options[i].coverage_units for i in ids), ids)
            )
    remaining = tuple(i for i in chosen if i not in immediate)
    pending = part(remaining, 2)
    if pending.approver is None:
        return blocked("NO_SUFFICIENT_APPROVER")
    parts = ((part(immediate, 1),) if immediate else ()) + (pending,)
    return Route(2, digest, parts)
