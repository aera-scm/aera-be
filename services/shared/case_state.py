"""Case state machine (SRD 6.4). Pure data; the case store enforces it."""

from services.shared.models import CaseStatus as S

TRANSITIONS: dict[S, frozenset[S]] = {
    S.RECEIVED: frozenset({S.TRIAGED}),
    S.TRIAGED: frozenset({S.INVESTIGATING}),
    # 6.4 lists WAITING_SUPPLIER as entered when request_supplier_info is sent, which
    # happens during an investigation, so INVESTIGATING leads to it (ADR-011).
    S.INVESTIGATING: frozenset(
        {S.WAITING_PLANNER, S.WAITING_SUPPLIER, S.PLAN_PROPOSED, S.ESCALATED}
    ),
    S.WAITING_PLANNER: frozenset({S.INVESTIGATING}),
    S.WAITING_SUPPLIER: frozenset({S.INVESTIGATING}),
    S.PLAN_PROPOSED: frozenset({S.VERIFIED, S.INVESTIGATING}),
    S.VERIFIED: frozenset({S.AUTO_APPROVED, S.AWAITING_APPROVAL, S.ESCALATED}),
    S.AWAITING_APPROVAL: frozenset({S.APPROVED, S.REJECTED}),
    S.AUTO_APPROVED: frozenset({S.EXECUTING}),
    S.APPROVED: frozenset({S.EXECUTING}),
    S.EXECUTING: frozenset({S.MONITORING, S.FAILED_ROLLED_BACK, S.INVESTIGATING}),
    S.MONITORING: frozenset({S.CLOSED, S.REOPENED}),
    S.REOPENED: frozenset({S.INVESTIGATING, S.ROLLED_BACK}),
    S.ESCALATED: frozenset({S.INVESTIGATING, S.CLOSED}),
    S.REJECTED: frozenset({S.INVESTIGATING, S.CLOSED}),
    S.CLOSED: frozenset(),
    S.ROLLED_BACK: frozenset(),
    S.FAILED_ROLLED_BACK: frozenset(),
}

TERMINAL: frozenset[S] = frozenset(state for state, targets in TRANSITIONS.items() if not targets)


def can_transition(source: S, target: S) -> bool:
    return target in TRANSITIONS[source]
