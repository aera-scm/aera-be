from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from unittest.mock import Mock

import pytest

from services.routing.logic import Policy, route
from services.shared.models import ApproverLimit, ProposedPlan
from services.verifier.grounding import evaluate
from services.verifier.logic import Corroboration, Donor, Grounding, OptionEvidence, verify

NOW = datetime(2026, 9, 24, 8, tzinfo=UTC)


def reference():
    options = []
    evidence = {}
    for oid, cost, qty, lead, action in [
        (
            "A",
            38200,
            640,
            8,
            {
                "type": "BOOK_AIR_FREIGHT",
                "supplierId": "S1",
                "poNumber": "PO1",
                "poItem": "10",
                "qty": 640,
                "arrival": NOW + timedelta(hours=8),
            },
        ),
        (
            "B",
            10000,
            1240,
            4,
            {
                "type": "CREATE_PO_ALTERNATE",
                "supplierId": "S2",
                "material": "M1",
                "plant": "1010",
                "qty": 1240,
                "deliveryDate": NOW.date(),
            },
        ),
        (
            "C",
            4100,
            600,
            3,
            {
                "type": "CREATE_STO",
                "fromPlant": "1020",
                "toPlant": "1010",
                "material": "M1",
                "qty": 600,
                "deliveryDate": NOW.date(),
            },
        ),
    ]:
        arrival = NOW + timedelta(hours=lead)
        figures = [
            {"name": "costUsd", "value": cost, "sourceRef": f"ratecard:{oid}"},
            {"name": "arrival", "value": str(arrival), "sourceRef": f"ratecard:{oid}"},
        ]
        options.append(
            dict(
                id=oid,
                name=oid,
                actions=[action],
                coverageUnits=qty,
                arrival=arrival,
                costUsd=cost,
                costSourceRef=f"ratecard:{oid}",
                figures=figures,
                rationale="Recorded synthetic case",
            )
        )
        evidence[oid] = OptionEvidence(
            NOW,
            "M1",
            "1010",
            (action,),
            D(cost),
            D(qty),
            arrival,
            NOW + timedelta(hours=6.2),
            D(1240),
            D(4720000),
            D(lead),
            reread={
                (f["sourceRef"], f["name"]): D(f["value"]) if f["name"] == "costUsd" else f["value"]
                for f in figures
            },
            plants=frozenset({"1010", "1020"}),
            suppliers=frozenset({"S1", "S2"}),
            compliance={"S2": "PENDING"},
            donors={("M1", "1020"): Donor(D(6000), D(100), D(1000), D(1200))},
            calendar_feasible=True,
        )
    plan = ProposedPlan.model_validate(
        dict(
            caseId="EXC-2026-0914",
            planVersion=1,
            options=options,
            chosen=["C", "A"],
            totalCostUsd=42300,
            coverageUnits=1240,
            rationale="Recorded synthetic case",
        )
    )
    return plan, evidence


def verified(plan=None, evidence=None, **kwargs):
    default_plan, default_evidence = reference()
    return verify(
        plan or default_plan,
        evidence or default_evidence,
        now=NOW,
        proposed_at=NOW,
        grounding=kwargs.get("grounding", Grounding(D("0.9"), D("0.9"), True)),
        corroboration=kwargs.get("corroboration", Corroboration(mrp_verified=True)),
    )


def limits():
    return [
        ApproverLimit(
            user_id=user,
            role="approver",
            plant="1010",
            limit_usd=D(amount),
            valid_from=NOW.date(),
            valid_to=NOW.date(),
        )
        for user, amount in [("primary", 50000), ("backup", 100000)]
    ]


def routed(value, **kwargs):
    return route(
        value, now=NOW, stockout=NOW + timedelta(hours=6.2), plant="1010", limits=limits(), **kwargs
    )


@pytest.mark.parametrize(
    "check,changes",
    [
        ("V-01", {"reread": {}}),
        ("V-03", {"need_at": NOW.replace(tzinfo=None)}),
        ("V-04", {"expected_actions": ()}),
        ("V-05", {"plants": frozenset()}),
        ("V-07", {"donors": {("M1", "1020"): Donor(D(1000), D(100), D(0), D(1000))}}),
        ("V-08", {"donors": {("M1", "1020"): Donor(D(6000), D(100), D(0), D(599))}}),
        ("V-09", {"rar_protected": D(4100)}),
        ("V-10", {"calendar_feasible": False}),
        ("V-11", {"recipients": frozenset({"attacker@example.invalid"})}),
        ("V-12", {"unconfirmed_fields": ("quantity",)}),
    ],
)
def test_checks_fail_closed(check, changes):
    plan, facts = reference()
    facts["C"] = replace(facts["C"], **changes)
    result = verified(plan, facts)
    assert any(
        c.check_id == check and c.option_id == "C" and not c.passed for c in result.record.checks
    )
    assert routed(result).tier == 3


def test_V_02_integer_positive_and_coverage_flags():
    plan, facts = reference()
    plan.options[2].actions[0].qty = D("0.5")
    assert not next(
        c
        for c in verified(plan, facts).record.checks
        if c.check_id == "V-02" and c.option_id == "C"
    ).passed
    result = verified()
    assert (
        next(c for c in result.record.checks if c.check_id == "V-02" and c.option_id == "C").detail
        == "Partial coverage"
    )


def test_V_06_BR_06_reference_B_blocked():
    result = verified()
    assert not next(
        c for c in result.record.checks if c.check_id == "V-06" and c.option_id == "B"
    ).passed
    assert routed(result).tier == 2


def test_V_13_BR_18_confidence():
    assert verified().record.confidence == D("0.98")
    assert verified(
        corroboration=Corroboration(agreeing_senders=frozenset({"S1"}))
    ).record.confidence == D("0.89")
    result = verified(grounding=Grounding())
    assert result.record.confidence == D("0.8")
    assert routed(result).tier == 3


def test_BR_05_BR_17_BR_22_reference_split_and_STO_only():
    value = routed(verified())
    assert value.tier == 2
    assert [(p.options, p.tier, p.cost) for p in value.parts] == [
        (("C",), 1, 4100),
        (("A",), 2, 38200),
    ]
    assert len({p.id for p in value.parts}) == 2
    plan, facts = reference()
    plan.chosen, plan.total_cost_usd, plan.coverage_units = ["C"], D(4100), D(600)
    assert routed(verified(plan, facts)).tier == 1
    facts["C"] = replace(facts["C"], cancellation_fee=D(1))
    assert routed(verified(plan, facts)).tier == 2


def test_BR_23_deadline_does_not_invent_time_for_late_freight():
    pending = routed(verified()).parts[-1]
    assert pending.deadline == NOW + timedelta(hours=6.2) - timedelta(hours=8)
    assert (pending.approver, pending.backup) == ("primary", "backup")


def test_BR_05_kill_switch_and_stale_hash():
    assert routed(verified(), policy=Policy(kill_switch=True)).reason.startswith("KILL_SWITCH")
    result = verified()
    result.record.plan.rationale = "changed"
    assert routed(result).reason == "STALE_VERIFICATION"


def test_FR_RTE_04_deterministic_sampling():
    plan, facts = reference()
    plan.chosen, plan.total_cost_usd, plan.coverage_units = ["C"], D(4100), D(600)
    assert routed(verified(plan, facts), policy=Policy(audit_share=D(1))).parts[0].sampled
    assert not routed(verified(plan, facts), policy=Policy(audit_share=D(0))).parts[0].sampled


def test_V_13_grounding_adapter_requires_both_scores():
    client = Mock()
    client.apply_guardrail.return_value = {
        "assessments": [
            {
                "contextualGroundingPolicy": {
                    "filters": [
                        {"type": "GROUNDING", "score": 0.9},
                        {"type": "RELEVANCE", "score": 0.8},
                    ]
                }
            }
        ]
    }
    result = evaluate(client, "guard", "1", rationale="rationale", source="SAP data", query="case")
    assert result == Grounding(D("0.9"), D("0.8"), True)
    client.apply_guardrail.return_value = {}
    assert not evaluate(
        client, "guard", "1", rationale="rationale", source="SAP data", query="case"
    ).available
