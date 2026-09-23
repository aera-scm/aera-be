"""Budget stack synthesis (NFR-COST-01, C-02).

The budget must exist before any other resource, including the resources that
``cdk bootstrap`` creates. These tests prove the stack is a single native budget
resource that needs no bootstrap, with the required actual-spend alerts.
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import Stack, assertions

from infra.budget_app import budget_stack_name, build_app
from infra.environments import EnvironmentRefusedError
from infra.stacks.budget import (
    ALERT_THRESHOLDS_PERCENT,
    ENV_BUDGET_LIMIT_USD,
    ENV_BUDGET_NAME,
    ENV_BUDGET_RECIPIENTS,
    BudgetSettings,
    BudgetSettingsError,
    settings_from_environment,
)

# Explicit test fixture values; real amount and recipients come from OT-09.
SETTINGS = BudgetSettings(
    name="aera-test-monthly",
    limit_usd=Decimal("100"),
    recipients=("alerts@example.com",),
)


def synth(settings: BudgetSettings = SETTINGS) -> assertions.Template:
    app = build_app(env_name="dev", settings=settings)
    stack = Stack.of(app.node.find_child(budget_stack_name("dev")))
    return assertions.Template.from_stack(stack)


def budget_properties(template: assertions.Template) -> dict[str, Any]:
    resources = template.find_resources("AWS::Budgets::Budget")
    assert len(resources) == 1
    properties: dict[str, Any] = next(iter(resources.values()))["Properties"]
    return properties


def test_nfr_cost_01_budget_is_monthly_usd_cost_budget() -> None:
    budget = budget_properties(synth())["Budget"]

    assert budget == {
        "BudgetName": "aera-test-monthly",
        "BudgetType": "COST",
        "TimeUnit": "MONTHLY",
        "BudgetLimit": {"Amount": 100, "Unit": "USD"},
    }


def test_nfr_cost_01_actual_spend_alerts_at_50_80_100_percent() -> None:
    alerts = budget_properties(synth())["NotificationsWithSubscribers"]

    assert [alert["Notification"] for alert in alerts] == [
        {
            "NotificationType": "ACTUAL",
            "ComparisonOperator": "GREATER_THAN",
            "Threshold": threshold,
            "ThresholdType": "PERCENTAGE",
        }
        for threshold in (50, 80, 100)
    ]
    assert ALERT_THRESHOLDS_PERCENT == (50, 80, 100)


def test_nfr_cost_01_every_recipient_subscribes_to_every_alert() -> None:
    settings = BudgetSettings(
        name="aera-test-monthly",
        limit_usd=Decimal("250.50"),
        recipients=("first@example.com", "second@example.com"),
    )

    properties = budget_properties(synth(settings))

    assert properties["Budget"]["BudgetLimit"] == {"Amount": 250.5, "Unit": "USD"}
    for alert in properties["NotificationsWithSubscribers"]:
        assert alert["Subscribers"] == [
            {"SubscriptionType": "EMAIL", "Address": "first@example.com"},
            {"SubscriptionType": "EMAIL", "Address": "second@example.com"},
        ]


def test_c_02_budget_stack_contains_only_the_budget() -> None:
    resources = synth().to_json()["Resources"]

    assert {resource["Type"] for resource in resources.values()} == {"AWS::Budgets::Budget"}


def test_c_02_budget_stack_needs_no_bootstrap() -> None:
    app = build_app(env_name="dev", settings=SETTINGS)
    assembly = app.synth()
    artifact = assembly.get_stack_by_name(budget_stack_name("dev"))
    template = artifact.template

    assert "BootstrapVersion" not in template.get("Parameters", {})
    assert "CheckBootstrapVersion" not in template.get("Rules", {})
    assert artifact.assets == []
    assert artifact.assume_role_arn is None
    assert artifact.cloud_formation_execution_role_arn is None
    assert artifact.requires_bootstrap_stack_version is None
    # The template itself must not be staged in the bootstrap assets bucket.
    assert artifact.stack_template_asset_object_url is None
    assert [dependency.id for dependency in artifact.dependencies] == []
    manifest = json.loads((Path(assembly.directory) / "manifest.json").read_text())
    artifact_types = {item["type"] for item in manifest["artifacts"].values()}
    assert "cdk:asset-manifest" not in artifact_types


def test_c_02_budget_app_holds_only_the_budget_stack() -> None:
    assembly = build_app(env_name="dev", settings=SETTINGS).synth()

    assert [stack.stack_name for stack in assembly.stacks] == ["aera-dev-budget"]


@pytest.mark.parametrize("env_name", ["final", "prod", ""])
def test_budget_app_refuses_non_dev_environments(env_name: str) -> None:
    with pytest.raises(EnvironmentRefusedError):
        build_app(env_name=env_name, settings=SETTINGS)


def test_nfr_cost_01_settings_are_read_from_environment() -> None:
    settings = settings_from_environment(
        {
            ENV_BUDGET_NAME: " aera-test-monthly ",
            ENV_BUDGET_LIMIT_USD: "120.00",
            ENV_BUDGET_RECIPIENTS: "a@example.com, b@example.com",
        }
    )

    assert settings == BudgetSettings(
        name="aera-test-monthly",
        limit_usd=Decimal("120.00"),
        recipients=("a@example.com", "b@example.com"),
    )


@pytest.mark.parametrize("missing", [ENV_BUDGET_NAME, ENV_BUDGET_LIMIT_USD, ENV_BUDGET_RECIPIENTS])
def test_nfr_cost_01_missing_budget_input_is_never_inferred(missing: str) -> None:
    environ = {
        ENV_BUDGET_NAME: "aera-test-monthly",
        ENV_BUDGET_LIMIT_USD: "100",
        ENV_BUDGET_RECIPIENTS: "alerts@example.com",
    }
    environ[missing] = "  "

    with pytest.raises(BudgetSettingsError, match=missing):
        settings_from_environment(environ)


@pytest.mark.parametrize("amount", ["0", "-5", "NaN", "Infinity", "ten"])
def test_nfr_cost_01_budget_amount_must_be_positive_number(amount: str) -> None:
    with pytest.raises(BudgetSettingsError, match="amount"):
        settings_from_environment(
            {
                ENV_BUDGET_NAME: "aera-test-monthly",
                ENV_BUDGET_LIMIT_USD: amount,
                ENV_BUDGET_RECIPIENTS: "alerts@example.com",
            }
        )


def test_nfr_cost_01_malformed_recipient_is_rejected_without_echoing_it() -> None:
    with pytest.raises(BudgetSettingsError) as error:
        BudgetSettings(
            name="aera-test-monthly",
            limit_usd=Decimal("100"),
            recipients=("alerts@example.com", "not-an-address"),
        )

    assert "recipient 2" in str(error.value)
    assert "not-an-address" not in str(error.value)


def test_nfr_cost_01_recipients_are_limited_to_the_budgets_maximum() -> None:
    recipients = tuple(f"person{index}@example.com" for index in range(11))

    with pytest.raises(BudgetSettingsError, match="at most 10"):
        BudgetSettings(name="aera-test-monthly", limit_usd=Decimal("100"), recipients=recipients)


@pytest.mark.parametrize("name", ["", "aera:monthly", "aera\\monthly", "x" * 101])
def test_nfr_cost_01_invalid_budget_name_is_rejected(name: str) -> None:
    with pytest.raises(BudgetSettingsError, match="name"):
        BudgetSettings(name=name, limit_usd=Decimal("100"), recipients=("alerts@example.com",))
