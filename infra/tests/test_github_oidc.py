"""NFR-SEC-03/06: exact GitHub claims and scoped bootstrap delegation."""

import json
from typing import Any

import pytest
from aws_cdk import App, Environment, Stack, assertions

from infra.app import DataSettings, build_app
from infra.constructs.github_oidc import GitHubOidc


def template(**overrides: Any) -> assertions.Template:
    app = App()
    stack = Stack(app, "test", env=Environment(account="123456789012", region="us-east-1"))
    settings = dict(
        env_name="dev",
        repository="example/backend",
        qualifier="aeradev",
        budget_name="synthetic-budget",
        provider_mode="create",
    )
    settings.update(overrides)
    GitHubOidc(stack, "GitHub", **settings)
    return assertions.Template.from_stack(stack)


def test_oidc_exact_audience_and_main_subject() -> None:
    result = template()
    roles = result.find_resources("AWS::IAM::Role")
    statements = next(iter(roles.values()))["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(statements) == 1
    trust = statements[0]
    assert trust["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert trust["Condition"] == {
        "StringEquals": {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": "repo:example/backend:ref:refs/heads/main",
        }
    }
    result.has_resource_properties(
        "AWS::IAM::OIDCProvider",
        {
            "Url": "https://token.actions.githubusercontent.com",
            "ClientIdList": ["sts.amazonaws.com"],
        },
    )


@pytest.mark.parametrize(
    "audience,subject",
    [
        ("wrong", "repo:example/backend:ref:refs/heads/main"),
        ("sts.amazonaws.com", "repo:other/backend:ref:refs/heads/main"),
        ("sts.amazonaws.com", "repo:example/backend:ref:refs/heads/feature"),
        ("sts.amazonaws.com", "repo:example/backend:pull_request"),
        ("sts.amazonaws.com", "repo:example/backend:environment:dev"),
    ],
)
def test_unapproved_claims_do_not_match(audience: str, subject: str) -> None:
    role = next(iter(template().find_resources("AWS::IAM::Role").values()))
    equals = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Condition"][
        "StringEquals"
    ]
    assert not (
        audience == equals["token.actions.githubusercontent.com:aud"]
        and subject == equals["token.actions.githubusercontent.com:sub"]
    )


def test_permissions_only_assume_approved_bootstrap_roles_and_read_budget() -> None:
    result = template().to_json()
    roles = [
        resource
        for resource in result["Resources"].values()
        if resource["Type"] == "AWS::IAM::Role"
    ]
    statements = roles[0]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assume = next(
        statement for statement in statements if statement["Action"] == ["sts:AssumeRole"]
    )
    encoded = json.dumps(assume["Resource"])
    for purpose in ("deploy", "file-publishing", "lookup"):
        assert f"cdk-aeradev-{purpose}-role-123456789012-us-east-1" in encoded
    assert "*" not in encoded
    assert len(assume["Resource"]) == 3
    assert "iam:PassRole" not in json.dumps(statements)
    assert "AdministratorAccess" not in json.dumps(result)
    assert "final" not in json.dumps(statements)
    assert all(statement["Resource"] != "*" for statement in statements)


@pytest.mark.parametrize(
    "settings",
    [
        {"env_name": "final"},
        {"repository": ""},
        {"repository": "example/*"},
        {"qualifier": "*"},
        {"qualifier": ""},
        {"budget_name": ""},
        {"provider_mode": ""},
    ],
)
def test_incomplete_or_unsafe_configuration_is_refused(settings: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        template(**settings)


def test_existing_provider_is_not_recreated() -> None:
    template(provider_mode="existing").resource_count_is("AWS::IAM::OIDCProvider", 0)


def test_main_app_omits_oidc_without_owner_inputs() -> None:
    app = build_app(DataSettings(env_name="dev", owner="synthetic-owner"))
    for artifact in app.synth().stacks:
        assert all(
            resource["Type"] not in {"AWS::IAM::Role", "AWS::IAM::OIDCProvider"}
            for resource in artifact.template["Resources"].values()
        )


def test_main_app_wires_oidc_and_qualifier_to_all_stacks() -> None:
    app = build_app(
        DataSettings(
            env_name="dev",
            owner="synthetic-owner",
            github_repository="example/backend",
            cdk_qualifier="aeradev",
            github_provider_mode="create",
            budget_name="synthetic-budget",
        )
    )
    assembly = app.synth()
    assert len(assembly.stacks) == 9
    for artifact in assembly.stacks:
        assert artifact.assume_role_arn is not None
        assert "cdk-aeradev-deploy-role" in artifact.assume_role_arn
    identity = assembly.get_stack_by_name("aera-dev-identity")
    assertions.Template.from_json(identity.template).resource_count_is("AWS::IAM::Role", 1)
