"""Cognito foundation; workload identities arrive with runtimes (SRD 6.16)."""

from typing import Any

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from constructs import Construct

from infra.environments import require_deployable_environment


class IdentityStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, env_name: str, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        require_deployable_environment(env_name)
        pool = cognito.CfnUserPool(
            self,
            "Pool",
            user_pool_name=f"aera-{env_name}-identity",
            admin_create_user_config=cognito.CfnUserPool.AdminCreateUserConfigProperty(
                allow_admin_create_user_only=True,
            ),
            username_attributes=["email"],
            auto_verified_attributes=["email"],
            username_configuration=cognito.CfnUserPool.UsernameConfigurationProperty(
                case_sensitive=False,
            ),
            deletion_protection="ACTIVE",
            policies=cognito.CfnUserPool.PoliciesProperty(
                password_policy=cognito.CfnUserPool.PasswordPolicyProperty(
                    minimum_length=12,
                    require_lowercase=True,
                    require_uppercase=True,
                    require_numbers=True,
                    require_symbols=True,
                    temporary_password_validity_days=1,
                ),
            ),
        )
        pool.apply_removal_policy(RemovalPolicy.RETAIN)
        for group in ("planner", "approver", "admin"):
            cognito.CfnUserPoolGroup(self, group, group_name=group, user_pool_id=pool.ref)
        client = cognito.CfnUserPoolClient(
            self,
            "ConsoleClient",
            user_pool_id=pool.ref,
            client_name=f"aera-{env_name}-web",
            generate_secret=False,
            allowed_o_auth_flows=["code"],
            allowed_o_auth_flows_user_pool_client=True,
            allowed_o_auth_scopes=["openid", "email", "profile"],
            supported_identity_providers=["COGNITO"],
            callback_ur_ls=["http://localhost:5173/callback"],
            logout_ur_ls=["http://localhost:5173/"],
            prevent_user_existence_errors="ENABLED",
            enable_token_revocation=True,
            explicit_auth_flows=["ALLOW_REFRESH_TOKEN_AUTH"],
        )
        cognito.CfnUserPoolDomain(
            self,
            "HostedDomain",
            user_pool_id=pool.ref,
            domain=f"aera-{env_name}-{self.account}-{self.region}",
        )
        CfnOutput(self, "UserPoolId", value=pool.ref)
        CfnOutput(self, "ClientId", value=client.ref)
