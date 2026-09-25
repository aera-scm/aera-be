"""Cognito foundation; workload identities arrive with runtimes (SRD 6.16)."""

from typing import Any

from aws_cdk import CfnOutput, Fn, RemovalPolicy, SecretValue, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
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
        self.pool_arn = pool.attr_arn
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
        resource_server = cognito.CfnUserPoolResourceServer(
            self,
            "InteropScopes",
            user_pool_id=pool.ref,
            identifier=f"aera-{env_name}-interop",
            name="AERA external agent service",
            scopes=[
                cognito.CfnUserPoolResourceServer.ResourceServerScopeTypeProperty(
                    scope_name="invoke", scope_description="Submit signals and read case results"
                )
            ],
        )
        self.interop_scope = f"aera-{env_name}-interop/invoke"
        machine_client = cognito.CfnUserPoolClient(
            self,
            "InteropClient",
            user_pool_id=pool.ref,
            client_name=f"aera-{env_name}-interop",
            generate_secret=True,
            allowed_o_auth_flows=["client_credentials"],
            allowed_o_auth_flows_user_pool_client=True,
            allowed_o_auth_scopes=[self.interop_scope],
            prevent_user_existence_errors="ENABLED",
            enable_token_revocation=True,
        )
        machine_client.add_dependency(resource_server)
        self.interop_client_id = machine_client.ref
        service_client = cognito.CfnUserPoolClient(
            self,
            "InteropServiceClient",
            user_pool_id=pool.ref,
            client_name=f"aera-{env_name}-interop-service",
            generate_secret=True,
            allowed_o_auth_flows=["client_credentials"],
            allowed_o_auth_flows_user_pool_client=True,
            allowed_o_auth_scopes=[self.interop_scope],
            prevent_user_existence_errors="ENABLED",
            enable_token_revocation=True,
        )
        service_client.add_dependency(resource_server)
        self.interop_service_client_id = service_client.ref
        self.interop_discovery_url = Fn.join(
            "",
            [
                f"https://cognito-idp.{self.region}.amazonaws.com/",
                pool.ref,
                "/.well-known/openid-configuration",
            ],
        )
        self.interop_provider = agentcore.OAuth2CredentialProvider.using_custom(
            self,
            "InteropProvider",
            o_auth2_credential_provider_name=f"aera_{env_name}_interop_service",
            client_id=service_client.ref,
            client_secret=SecretValue.resource_attribute(service_client.attr_client_secret),
            discovery_url=self.interop_discovery_url,
        )
        cognito.CfnUserPoolDomain(
            self,
            "HostedDomain",
            user_pool_id=pool.ref,
            # The Hosted UI URL is public, so it must not carry the account id. The first
            # block of the stack GUID is unique per stack and stable for its lifetime.
            domain=Fn.join(
                "",
                [
                    f"aera-{env_name}-",
                    Fn.select(0, Fn.split("-", Fn.select(2, Fn.split("/", self.stack_id)))),
                ],
            ),
        )
        CfnOutput(self, "UserPoolId", value=pool.ref)
        CfnOutput(self, "ClientId", value=client.ref)
        CfnOutput(self, "InteropClientId", value=machine_client.ref)
