"""Optional, explicitly configured GitHub OIDC delegation (NFR-SEC-03/06)."""

import re

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

from infra.environments import APPROVED_REGION, require_deployable_environment


class GitHubOidc(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        repository: str,
        qualifier: str,
        budget_name: str,
        provider_mode: str,
    ) -> None:
        super().__init__(scope, construct_id)
        require_deployable_environment(env_name)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("An exact approved owner/repository is required")
        if not re.fullmatch(r"[a-zA-Z0-9]{1,10}", qualifier):
            raise ValueError("An approved CDK qualifier is required")
        if not budget_name or any(char in budget_name for char in "*?\r\n"):
            raise ValueError("An exact budget name is required")
        stack = Stack.of(self)
        if stack.region != APPROVED_REGION:
            raise ValueError("OIDC deployment is restricted to the approved region")
        if provider_mode == "create":
            provider = iam.CfnOIDCProvider(
                self,
                "Provider",
                url="https://token.actions.githubusercontent.com",
                client_id_list=["sts.amazonaws.com"],
            )
            provider_arn = provider.attr_arn
        elif provider_mode == "existing":
            provider_arn = (
                f"arn:{stack.partition}:iam::{stack.account}:"
                "oidc-provider/token.actions.githubusercontent.com"
            )
        else:
            raise ValueError("Choose create or existing for the approved OIDC provider")
        bootstrap_roles = [
            f"arn:{stack.partition}:iam::{stack.account}:role/cdk-{qualifier}-{purpose}-role-{stack.account}-{stack.region}"
            for purpose in ("deploy", "file-publishing", "image-publishing", "lookup")
        ]
        subject = f"repo:{repository}:ref:refs/heads/main"
        budget_arn = f"arn:{stack.partition}:budgets::{stack.account}:budget/{budget_name}"
        version_arn = (
            f"arn:{stack.partition}:ssm:{stack.region}:{stack.account}:"
            f"parameter/cdk-bootstrap/{qualifier}/version"
        )
        self.role = iam.CfnRole(
            self,
            "DeployRole",
            role_name=f"aera-{env_name}-github-deploy",
            max_session_duration=3600,
            assume_role_policy_document={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "sts:AssumeRoleWithWebIdentity",
                        "Principal": {"Federated": provider_arn},
                        "Condition": {
                            "StringEquals": {
                                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                                "token.actions.githubusercontent.com:sub": subject,
                            }
                        },
                    }
                ],
            },
            policies=[
                iam.CfnRole.PolicyProperty(
                    policy_name="FoundationDeployment",
                    policy_document={
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": ["sts:AssumeRole"],
                                "Resource": bootstrap_roles,
                            },
                            {
                                "Effect": "Allow",
                                "Action": ["budgets:ViewBudget"],
                                "Resource": budget_arn,
                            },
                            {
                                "Effect": "Allow",
                                "Action": ["ssm:GetParameter"],
                                "Resource": version_arn,
                            },
                        ],
                    },
                )
            ],
        )
        CfnOutput(self, "DeployRoleArn", value=self.role.attr_arn)
