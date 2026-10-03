from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import Any
from unittest.mock import Mock

import pytest

from services.routing.logic import Policy, Route, route
from services.shared.models import ApproverLimit, ProposedPlan
from services.verifier.grounding import evaluate, sentences
from services.verifier.logic import (
    Corroboration,
    Donor,
    Grounding,
    OptionEvidence,
    Verification,
    verify,
)

NOW = datetime(2026, 9, 24, 8, tzinfo=UTC)


def reference() -> tuple[ProposedPlan, dict[str, OptionEvidence]]:
    options = []
    evidence = {}
    rows: list[tuple[str, int, int, int, dict[str, Any]]] = [
        (
            "A",
            38200,
            640,
            17,
            {
                "type": "BOOK_AIR_FREIGHT",
                "supplierId": "S1",
                "poNumber": "PO1",
                "poItem": "10",
                "qty": 640,
                "arrival": NOW + timedelta(hours=17),
            },
        ),
        (
            "B",
            10000,
            1240,
            36,
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
            5,
            {
                "type": "CREATE_STO",
                "fromPlant": "1020",
                "toPlant": "1010",
                "material": "M1",
                "qty": 600,
                "deliveryDate": NOW.date(),
            },
        ),
    ]
    for oid, cost, qty, lead, action in rows:
        arrival = NOW + timedelta(hours=lead)
        figures: list[dict[str, Any]] = [
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


def verified(
    plan: ProposedPlan | None = None,
    evidence: dict[str, OptionEvidence] | None = None,
    **kwargs: Any,
) -> Verification:
    default_plan, default_evidence = reference()
    return verify(
        plan or default_plan,
        evidence or default_evidence,
        now=NOW,
        proposed_at=NOW,
        grounding=kwargs.get("grounding", Grounding(D("0.9"), D("0.9"), True)),
        corroboration=kwargs.get("corroboration", Corroboration(mrp_verified=True)),
    )


def limits() -> list[ApproverLimit]:
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


def routed(value: Verification, **kwargs: Any) -> Route:
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
def test_checks_fail_closed(check: str, changes: dict[str, Any]) -> None:
    plan, facts = reference()
    facts["C"] = replace(facts["C"], **changes)
    result = verified(plan, facts)
    assert any(
        c.check_id == check and c.option_id == "C" and not c.passed for c in result.record.checks
    )
    assert routed(result).tier == 3


def v10(minutes_later: int, priced_at: datetime) -> bool:
    """V-10 on option C (5 h lead, arrival NOW + 5 h), verified some minutes after NOW."""
    plan, facts = reference()
    now = NOW + timedelta(minutes=minutes_later)
    facts = {oid: replace(f, read_at=now, priced_at=priced_at) for oid, f in facts.items()}
    result = verify(
        plan,
        facts,
        now=now,
        proposed_at=NOW,
        grounding=Grounding(D("0.9"), D("0.9"), True),
        corroboration=Corroboration(mrp_verified=True),
    )
    return next(
        c for c in result.record.checks if c.check_id == "V-10" and c.option_id == "C"
    ).passed


def test_V_10_lead_time_is_measured_from_the_pricing_moment() -> None:
    assert v10(2, NOW)  # priced two minutes ago, arrival = priced + lead
    assert not v10(16, NOW)  # price older than 15 minutes must be re-priced (ADR-0029)
    assert not v10(2, NOW + timedelta(minutes=1))  # arrival earlier than the lead allows
    assert not v10(2, NOW + timedelta(minutes=3))  # priced in the future


def test_FR_VER_02_failed_checks_name_their_reason() -> None:
    plan, facts = reference()
    facts["C"] = replace(
        facts["C"],
        donors={("M1", "1020"): Donor(D(1000), D(100), D(0), D(1000))},
        reread={},
    )
    checks = {
        c.check_id: c.detail
        for c in verified(plan, facts).record.checks
        if c.option_id == "C" and not c.passed
    }
    assert "costUsd 4100 differs from re-read nothing" in checks["V-01"]
    assert "donor 1020: 400 left after the transfer, minimum cover needs 4800" in checks["V-07"]
    late = verify(
        plan,
        {oid: replace(f, read_at=NOW + timedelta(minutes=20)) for oid, f in reference()[1].items()},
        now=NOW + timedelta(minutes=20),
        proposed_at=NOW,
        grounding=Grounding(D("0.9"), D("0.9"), True),
        corroboration=Corroboration(mrp_verified=True),
    )
    [v10] = [c for c in late.record.checks if c.check_id == "V-10" and c.option_id == "C"]
    assert not v10.passed and "earliest with 5 h lead" in v10.detail


def test_V_02_integer_positive_and_coverage_flags() -> None:
    plan, facts = reference()
    action = plan.options[2].actions[0]
    assert action.type == "CREATE_STO"
    action.qty = D("0.5")
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


def test_V_06_BR_06_reference_B_blocked() -> None:
    result = verified()
    assert not next(
        c for c in result.record.checks if c.check_id == "V-06" and c.option_id == "B"
    ).passed
    assert routed(result).tier == 2


def test_V_13_BR_18_confidence() -> None:
    assert verified().record.confidence == D("0.98")
    assert verified(
        corroboration=Corroboration(agreeing_senders=frozenset({"S1"}))
    ).record.confidence == D("0.89")
    result = verified(grounding=Grounding())
    assert result.record.confidence == D("0.8")
    assert routed(result).tier == 3


def test_BR_05_BR_17_BR_22_reference_split_and_STO_only() -> None:
    value = routed(verified())
    assert value.tier == 2
    assert [(p.options, p.tier, p.cost) for p in value.parts] == [
        (("C",), 1, D(4100)),
        (("A",), 2, D(38200)),
    ]
    assert len({p.id for p in value.parts}) == 2
    plan, facts = reference()
    plan.chosen, plan.total_cost_usd, plan.coverage_units = ["C"], D(4100), D(600)
    assert routed(verified(plan, facts)).tier == 1
    facts["C"] = replace(facts["C"], cancellation_fee=D(1))
    assert routed(verified(plan, facts)).tier == 2


def test_BR_23_deadline_does_not_invent_time_for_late_freight() -> None:
    pending = routed(verified()).parts[-1]
    assert pending.deadline == NOW + timedelta(hours=6.2) - timedelta(hours=17)
    assert (pending.approver, pending.backup) == ("primary", "backup")


def test_BR_05_kill_switch_and_stale_hash() -> None:
    reason = routed(verified(), policy=Policy(kill_switch=True)).reason
    assert reason is not None and reason.startswith("KILL_SWITCH")
    result = verified()
    result.record.plan.rationale = "changed"
    assert routed(result).reason == "STALE_VERIFICATION"


def test_FR_RTE_04_deterministic_sampling() -> None:
    plan, facts = reference()
    plan.chosen, plan.total_cost_usd, plan.coverage_units = ["C"], D(4100), D(600)
    assert routed(verified(plan, facts), policy=Policy(audit_share=D(1))).parts[0].sampled
    assert not routed(verified(plan, facts), policy=Policy(audit_share=D(0))).parts[0].sampled


def test_V_13_grounding_adapter_requires_both_scores() -> None:
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


def test_V_13_BR_18_grounding_is_the_mean_of_sentence_scores() -> None:
    """ADR-0033: G = mean GROUNDING over the rationale's sentences; RELEVANCE from the whole
    rationale; one failed call makes the whole check unavailable."""
    whole = "Option A ships 600 PC. Option B flies 640 PC (ratecard:RC-AIR)."
    scores = {
        whole: (0.3, 0.9),
        "Option A ships 600 PC.": (0.8, 0.2),
        "Option B flies 640 PC (ratecard:RC-AIR).": (0.6, 0.1),
    }

    class Client:
        def __init__(self, fail_on: str = "") -> None:
            self.fail_on = fail_on

        def apply_guardrail(self, **request: Any) -> dict[str, Any]:
            text = request["content"][2]["text"]["text"]
            if text == self.fail_on:
                return {}
            grounding, relevance = scores[text]
            return {
                "assessments": [
                    {
                        "contextualGroundingPolicy": {
                            "filters": [
                                {"type": "GROUNDING", "score": grounding},
                                {"type": "RELEVANCE", "score": relevance},
                            ]
                        }
                    }
                ]
            }

    result = evaluate(Client(), "guard", "1", rationale=whole, source="SAP data", query="case")
    assert result == Grounding(D("0.7"), D("0.9"), True)
    failed = evaluate(
        Client(fail_on="Option A ships 600 PC."),
        "guard",
        "1",
        rationale=whole,
        source="SAP data",
        query="case",
    )
    assert not failed.available
    assert sentences("Costs 6.2 h. Next one.") == ["Costs 6.2 h.", "Next one."]
