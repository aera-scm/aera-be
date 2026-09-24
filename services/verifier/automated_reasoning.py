"""FR-VER-04: second policy check of deterministic approval claims."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from services.routing.logic import Policy, Route
from services.verifier.logic import Verification, is_reversible


@dataclass(frozen=True)
class PolicyAssessment:
    status: str
    statements: str
    findings: tuple[str, ...]
    policy_arn: str | None

    @property
    def disagrees(self) -> bool:
        return self.status in {"INVALID", "AMBIGUOUS", "UNAVAILABLE"}


def decisive_statements(
    verified: Verification, proposed: Route, policy: Policy | None = None
) -> str:
    plan = verified.record.plan
    options = {option.id: option for option in plan.options}
    policy = policy or Policy()
    reversible = (
        all(oid in options and oid in verified.evidence for oid in plan.chosen)
        and all(
            is_reversible(options[oid], verified.evidence[oid]) for oid in plan.chosen
        )
    )
    donor_checks = [
        check for check in verified.record.checks
        if check.check_id == "V-07" and (check.option_id is None or check.option_id in plan.chosen)
    ]
    donor_ok = bool(donor_checks) and all(check.passed for check in donor_checks)
    protected = min(
        (verified.evidence[oid].rar_protected for oid in plan.chosen
         if oid in verified.evidence),
        default=0,
    )
    lines = [
        f"Case {plan.case_id}. The proposed autonomy tier is {proposed.tier}.",
        f"Total chosen action cost in US cents is {int(plan.total_cost_usd * 100)}.",
        f"The Tier 1 auto limit in US cents is {int(policy.auto_limit * 100)}.",
        f"Verifier confidence percent is "
        f"{int(verified.confidence_for(tuple(plan.chosen)) * 100)}.",
        f"Tier 1 minimum confidence percent is {int(policy.confidence_min * 100)}.",
        f"All chosen actions are {'reversible' if reversible else 'not reversible'}.",
        f"Donor cover and customer commitments check V-07 {'passed' if donor_ok else 'failed'}.",
        f"Revenue at risk protected by the chosen plan in US cents is {int(protected * 100)}.",
    ]
    for option_id in plan.chosen:
        option = options[option_id]
        facts = verified.evidence.get(option_id)
        if facts is None:
            lines.append(f"Option {option_id} lacks fresh source evidence.")
            continue
        reversibility = "reversible" if is_reversible(option, facts) else "irreversible"
        lines.extend([
            f"Option {option_id} costs USD {option.cost_usd}.",
            f"Option {option_id} is {reversibility}.",
            f"Option {option_id} protects USD {facts.rar_protected} revenue at risk.",
        ])
        for (material, plant), donor in sorted(facts.donors.items()):
            lines.append(
                f"Donor plant {plant} material {material} has {donor.on_hand} units, "
                f"{donor.unreserved} unreserved units, and {donor.customer_commitments} "
                "units of customer commitments."
            )
    return "\n".join(lines)


def assess(
    client: Any, guardrail_id: str, version: str, policy_arn: str | None,
    verified: Verification, proposed: Route, policy: Policy | None = None,
) -> PolicyAssessment:
    statements = decisive_statements(verified, proposed, policy)
    if not guardrail_id or not version or not policy_arn:
        return PolicyAssessment("UNAVAILABLE", statements, (), policy_arn)
    try:
        response = client.apply_guardrail(
            guardrailIdentifier=guardrail_id, guardrailVersion=version,
            source="OUTPUT", outputScope="FULL",
            content=[{"text": {"text": statements}}],
        )
        findings = tuple(
            str(kind).upper()
            for assessment in response.get("assessments", [])
            for finding in assessment.get("automatedReasoningPolicy", {}).get("findings", [])
            for kind, payload in finding.items() if payload is not None
        )
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError):
        return PolicyAssessment("UNAVAILABLE", statements, (), policy_arn)
    if not findings:
        return PolicyAssessment("UNAVAILABLE", statements, (), policy_arn)
    status = (
        "INVALID" if any(kind in {"INVALID", "IMPOSSIBLE"} for kind in findings)
        else "VALID" if set(findings) == {"VALID"}
        else "AMBIGUOUS"
    )
    return PolicyAssessment(status, statements, findings, policy_arn)
