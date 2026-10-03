"""Cognito foundation: NFR-SEC-03/05, NFR-MNT-02, SRD 6.19."""

import pytest
from aws_cdk import Stack, assertions

from infra.app import DataSettings, build_app


@pytest.fixture(scope="module")
def template() -> assertions.Template:
    app = build_app(DataSettings(env_name="dev", owner="synthetic-owner"))
    return assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-identity")))


def test_cognito_groups_and_no_users(template: assertions.Template) -> None:
    groups = template.find_resources("AWS::Cognito::UserPoolGroup")
    assert {group["Properties"]["GroupName"] for group in groups.values()} == {
        "planner",
        "approver",
        "admin",
    }
    template.resource_count_is("AWS::Cognito::UserPool", 1)
    template.resource_count_is("AWS::Cognito::UserPoolUser", 0)
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "UserPoolName": "aera-dev-identity",
            "DeletionProtection": "ACTIVE",
            "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True},
        },
    )


def test_public_code_client_is_pkce_compatible(template: assertions.Template) -> None:
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {
            "GenerateSecret": False,
            "AllowedOAuthFlows": ["code"],
            "AllowedOAuthFlowsUserPoolClient": True,
            "AllowedOAuthScopes": ["openid", "email", "profile"],
            "CallbackURLs": ["http://localhost:5173/callback"],
            "LogoutURLs": ["http://localhost:5173/"],
            "PreventUserExistenceErrors": "ENABLED",
            "EnableTokenRevocation": True,
        },
    )
    template.resource_count_is("AWS::Cognito::UserPoolDomain", 1)


def test_hosted_ui_domain_does_not_publish_the_account_id(template: assertions.Template) -> None:
    # The Hosted UI URL is public; derive uniqueness from the stack id instead.
    domains = template.find_resources("AWS::Cognito::UserPoolDomain")
    domain = next(iter(domains.values()))["Properties"]["Domain"]
    text = str(domain)

    assert "AWS::AccountId" not in text
    assert "AWS::StackId" in text
    assert domain["Fn::Join"][1][0] == "aera-dev-"


def test_nfr_sec_04_hosted_console_origin_is_shared_by_cognito_and_cors() -> None:
    app = build_app(
        DataSettings(
            env_name="dev",
            owner="synthetic-owner",
            console_origins=("https://console.example.invalid",),
        )
    )
    identity = assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-identity")))
    identity.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {
            "GenerateSecret": False,
            "CallbackURLs": ["https://console.example.invalid/callback"],
            "LogoutURLs": ["https://console.example.invalid/"],
        },
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://console.example.invalid",
        "https://console.example.invalid/callback",
        "https://user@console.example.invalid",
        "https://console.example.invalid?token=x",
        "*",
    ],
)
def test_nfr_sec_04_refuses_unsafe_console_origins(origin: str) -> None:
    with pytest.raises(ValueError, match="console origin"):
        build_app(DataSettings(env_name="dev", owner="synthetic-owner", console_origins=(origin,)))
