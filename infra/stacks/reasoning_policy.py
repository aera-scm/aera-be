"""FR-VER-04: English-only formal policy over approval, donor cover and RaR."""

from __future__ import annotations

from aws_cdk import Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_ssm as ssm


def add_reasoning_policy(stack: Stack, env_name: str) -> str:
    # The service accepts SMT-LIB prefix expressions and upper-case built-in types only;
    # anything else is refused at creation with "Policy is not valid".
    variable = bedrock.CfnAutomatedReasoningPolicy.PolicyDefinitionVariableProperty
    rule = bedrock.CfnAutomatedReasoningPolicy.PolicyDefinitionRuleProperty
    variables = [
        variable(name="tier", type="INT", description="Proposed autonomy tier."),
        variable(
            name="total_cost_cents",
            type="INT",
            description="Total selected action cost in US cents.",
        ),
        variable(
            name="auto_limit_cents",
            type="INT",
            description="Configured Tier 1 cost ceiling in US cents.",
        ),
        variable(name="confidence", type="INT", description="Verifier confidence in percent."),
        variable(
            name="confidence_min", type="INT", description="Tier 1 minimum confidence percent."
        ),
        variable(
            name="all_reversible", type="BOOL", description="All chosen actions can be undone."
        ),
        variable(
            name="donor_protection_ok",
            type="BOOL",
            description="Verifier V-07 confirms donor cover and customer commitments.",
        ),
        variable(
            name="rar_protected_cents",
            type="INT",
            description="US cents revenue at risk protected by the plan.",
        ),
    ]
    rules = [
        rule(
            id="BR05AUTO0001",
            expression=(
                "(=> (= tier 1) (and (<= total_cost_cents auto_limit_cents) "
                "(>= confidence confidence_min) all_reversible))"
            ),
        ),
        rule(id="BR07DONOR001", expression="(=> (or (= tier 1) (= tier 2)) donor_protection_ok)"),
        rule(
            id="BR12RAR00001",
            expression="(=> (or (= tier 1) (= tier 2)) (< total_cost_cents rar_protected_cents))",
        ),
    ]
    policy = bedrock.CfnAutomatedReasoningPolicy(
        stack,
        "ApprovalReasoningPolicy",
        name=f"aera-{env_name}-approval-policy",
        description="Second check of BR-05, BR-07 and BR-12; reports findings only.",
        policy_definition=bedrock.CfnAutomatedReasoningPolicy.PolicyDefinitionProperty(
            rules=rules,
            types=[],
            variables=variables,
            version="1",
        ),
        kms_key_id=None,
    )
    for key, value in (("REASONING_POLICY_ARN", policy.attr_policy_arn),):
        ssm.StringParameter(
            stack,
            f"Param{key}",
            parameter_name=f"/aera/{env_name}/{key}",
            string_value=value,
        )
    return policy.attr_policy_arn
