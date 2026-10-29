"""Cognito foundation; workload identities arrive with runtimes (SRD 6.16)."""

import base64
from pathlib import Path
from typing import Any

from aws_cdk import CfnOutput, Fn, RemovalPolicy, SecretValue, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cognito as cognito
from constructs import Construct

from infra.environments import require_deployable_environment

_LOGIN_ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "login"
_LOGIN_ASSETS = (
    ("FORM_LOGO", "PNG", "form-logo.png"),
    ("PAGE_BACKGROUND", "SVG", "page-background.svg"),
)
_BLUE, _BLUE_DARK, _NAVY, _MUTED = "2868e8ff", "1554d1ff", "152749ff", "5c6d88ff"
# Sign-in page styling in the console's palette; Cognito merges unset keys with its defaults.
_LOGIN_SETTINGS: dict[str, Any] = {
    "categories": {
        "global": {
            "colorSchemeMode": "LIGHT",
            "pageHeader": {"enabled": False},
            "pageFooter": {"enabled": False},
            "spacingDensity": "REGULAR",
        },
        "form": {
            "displayGraphics": True,
            "instructions": {"enabled": False},
            "languageSelector": {"enabled": False},
            "location": {"horizontal": "CENTER", "vertical": "CENTER"},
            "sessionTimerDisplay": "NONE",
        },
    },
    "components": {
        "pageBackground": {"image": {"enabled": True}, "lightMode": {"color": "f7f9fcff"}},
        "form": {
            "borderRadius": 20,
            "backgroundImage": {"enabled": False},
            "logo": {
                "enabled": True,
                "location": "CENTER",
                "position": "TOP",
                "formInclusion": "IN",
            },
            "lightMode": {"backgroundColor": "ffffffff", "borderColor": "dfe8f6ff"},
        },
        "pageText": {
            "lightMode": {"headingColor": _NAVY, "bodyColor": _MUTED, "descriptionColor": _MUTED}
        },
        "primaryButton": {
            "lightMode": {
                "defaults": {"backgroundColor": _BLUE, "textColor": "ffffffff"},
                "hover": {"backgroundColor": _BLUE_DARK, "textColor": "ffffffff"},
                "active": {"backgroundColor": _BLUE_DARK, "textColor": "ffffffff"},
                "disabled": {"backgroundColor": "c9d7eeff", "borderColor": "c9d7eeff"},
            }
        },
        "secondaryButton": {
            "lightMode": {
                "defaults": {
                    "backgroundColor": "ffffffff",
                    "borderColor": "dce4efff",
                    "textColor": "485b79ff",
                },
                "hover": {"backgroundColor": "f0f5ffff", "borderColor": _BLUE, "textColor": _BLUE},
                "active": {
                    "backgroundColor": "e4eeffff",
                    "borderColor": _BLUE_DARK,
                    "textColor": _BLUE_DARK,
                },
            }
        },
    },
    "componentClasses": {
        "buttons": {"borderRadius": 10},
        "input": {
            "borderRadius": 10,
            "lightMode": {
                "defaults": {"backgroundColor": "fcfdffff", "borderColor": "d7e1efff"},
                "placeholderColor": "8a96a8ff",
            },
        },
        "inputLabel": {"lightMode": {"textColor": "344b6bff"}},
        "focusState": {"lightMode": {"borderColor": _BLUE}},
        "link": {
            "lightMode": {"defaults": {"textColor": _BLUE}, "hover": {"textColor": _BLUE_DARK}}
        },
        "divider": {"lightMode": {"borderColor": "e8edf5ff"}},
    },
}


def _asset(name: str) -> str:
    return base64.b64encode((_LOGIN_ASSET_DIR / name).read_bytes()).decode("ascii")


class IdentityStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        console_origins: tuple[str, ...] = ("http://localhost:5173",),
        **kwargs: Any,
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
            callback_ur_ls=[f"{origin}/callback" for origin in console_origins],
            logout_ur_ls=[f"{origin}/" for origin in console_origins],
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
        # The Hosted UI URL is public, so it must not carry the account id. The first
        # block of the stack GUID is unique per stack and stable for its lifetime.
        domain = Fn.join(
            "",
            [
                f"aera-{env_name}-",
                Fn.select(0, Fn.split("-", Fn.select(2, Fn.split("/", self.stack_id)))),
            ],
        )
        # Managed login (version 2) is the current Hosted UI; the same /oauth2 endpoints and
        # PKCE flow apply, only the sign-in pages carry the AERA branding below.
        hosted_domain = cognito.CfnUserPoolDomain(
            self, "HostedDomain", user_pool_id=pool.ref, domain=domain, managed_login_version=2
        )
        branding = cognito.CfnManagedLoginBranding(
            self,
            "ConsoleBranding",
            user_pool_id=pool.ref,
            client_id=client.ref,
            settings=_LOGIN_SETTINGS,
            assets=[
                cognito.CfnManagedLoginBranding.AssetTypeProperty(
                    category=category, color_mode="LIGHT", extension=extension, bytes=_asset(name)
                )
                for category, extension, name in _LOGIN_ASSETS
            ],
        )
        branding.add_dependency(hosted_domain)
        self.pool_id = pool.ref
        self.issuer = Fn.join("", [f"https://cognito-idp.{self.region}.amazonaws.com/", pool.ref])
        self.token_url = Fn.join(
            "", ["https://", domain, f".auth.{self.region}.amazoncognito.com/oauth2/token"]
        )
        CfnOutput(self, "UserPoolId", value=pool.ref)
        CfnOutput(self, "ClientId", value=client.ref)
        CfnOutput(self, "InteropClientId", value=machine_client.ref)
