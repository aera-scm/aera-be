"""FR-VER-04: second policy check of deterministic approval claims.

The plan's decisive facts go to the guardrail as premises (`query`) and the proposed tier as
the one claim (`guard_content`), so the policy answers one question: may this plan take this
tier? `SATISFIABLE` means the tier is allowed by the facts (agreement); `INVALID` means the
facts forbid it (disagreement). Only facts the policy has variables for are sent; anything
else makes the service return no finding at all (ADR-0030).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from services.routing.logic import Policy, Route
from services.verifier.logic import Verification, is_reversible

LOG = logging.getLogger(__name__)
AGREEING = frozenset({"VALID", "SATISFIABLE"})


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
    premises, claim = decisive_facts(verified, proposed, policy)
    return f"{premises}\n{claim}"


def decisive_facts(
    verified: Verification, proposed: Route, policy: Policy | None = None
) -> tuple[str, str]:
    """(premises, claim): one English sentence per policy variable, then the tier."""
    plan = verified.record.plan
    options = {option.id: option for option in plan.options}
    policy = policy or Policy()
    reversible = all(oid in options and oid in verified.evidence for oid in plan.chosen) and all(
        is_reversible(options[oid], verified.evidence[oid]) for oid in plan.chosen
    )
    donor_checks = [
        check
        for check in verified.record.checks
        if check.check_id == "V-07" and (check.option_id is None or check.option_id in plan.chosen)
    ]
    donor_ok = bool(donor_checks) and all(check.passed for check in donor_checks)
    protected = min(
        (verified.evidence[oid].rar_protected for oid in plan.chosen if oid in verified.evidence),
        default=0,
    )
    premises = [
        f"Total chosen action cost in US cents is {int(plan.total_cost_usd * 100)}.",
        f"The Tier 1 auto limit in US cents is {int(policy.auto_limit * 100)}.",
        f"Verifier confidence percent is {int(verified.confidence_for(tuple(plan.chosen)) * 100)}.",
        f"Tier 1 minimum confidence percent is {int(policy.confidence_min * 100)}.",
        f"All chosen actions are {'reversible' if reversible else 'not reversible'}.",
        f"Donor cover and customer commitments check V-07 {'passed' if donor_ok else 'failed'}.",
        f"Revenue at risk protected by the chosen plan in US cents is {int(protected * 100)}.",
    ]
    return "\n".join(premises), f"The proposed autonomy tier is {proposed.tier}."


def assess(
    client: Any,
    guardrail_id: str,
    version: str,
    policy_arn: str | None,
    verified: Verification,
    proposed: Route,
    policy: Policy | None = None,
) -> PolicyAssessment:
    premises, claim = decisive_facts(verified, proposed, policy)
    statements = f"{premises}\n{claim}"
    if not guardrail_id or not version or not policy_arn:
        LOG.warning("Automated Reasoning check not configured")
        return PolicyAssessment("UNAVAILABLE", statements, (), policy_arn)
    try:
        response = client.apply_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=version,
            source="OUTPUT",
            outputScope="FULL",
            content=[
                {"text": {"text": premises, "qualifiers": ["query"]}},
                {"text": {"text": claim, "qualifiers": ["guard_content"]}},
            ],
        )
        findings = tuple(
            str(kind).upper()
            for assessment in response.get("assessments", [])
            for finding in assessment.get("automatedReasoningPolicy", {}).get("findings", [])
            for kind, payload in finding.items()
            if payload is not None
        )
    except ClientError as error:
        detail = error.response.get("Error", {})
        # The service message names the denied action and resource; never the statements.
        LOG.warning(
            "Automated Reasoning check unavailable: %s %s",
            detail.get("Code", "unknown"),
            str(detail.get("Message", ""))[:500],
        )
        return PolicyAssessment("UNAVAILABLE", statements, (), policy_arn)
    except (BotoCoreError, KeyError, TypeError, ValueError) as error:
        LOG.warning("Automated Reasoning check unavailable: %s", type(error).__name__)
        return PolicyAssessment("UNAVAILABLE", statements, (), policy_arn)
    if not findings:
        LOG.warning("Automated Reasoning check returned no finding")
        return PolicyAssessment("UNAVAILABLE", statements, (), policy_arn)
    status = (
        "INVALID"
        if any(kind in {"INVALID", "IMPOSSIBLE"} for kind in findings)
        else "VALID"
        if set(findings) == {"VALID"}
        else "CONSISTENT"
        if set(findings) <= AGREEING
        else "AMBIGUOUS"
    )
    return PolicyAssessment(status, statements, findings, policy_arn)
