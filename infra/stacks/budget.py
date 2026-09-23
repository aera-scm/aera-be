"""Monthly cost budget with actual-spend alerts (NFR-COST-01, C-02).

The stack holds one native CloudFormation budget resource and nothing else, so
it can be deployed with CLI credentials before ``cdk bootstrap``
creates any resource. Amount and recipients are operator inputs; nothing here
infers them.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from aws_cdk import Stack
from aws_cdk import aws_budgets as budgets
from constructs import Construct

ALERT_THRESHOLDS_PERCENT: tuple[int, ...] = (50, 80, 100)
# AWS Budgets accepts at most ten email subscribers per notification.
MAX_RECIPIENTS = 10

ENV_BUDGET_NAME = "AERA_BUDGET_NAME"
ENV_BUDGET_LIMIT_USD = "AERA_BUDGET_LIMIT_USD"
ENV_BUDGET_RECIPIENTS = "AERA_BUDGET_RECIPIENTS"

_BUDGET_NAME = re.compile(r"[^:\\]{1,100}")
_EMAIL = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+")


class BudgetSettingsError(ValueError):
    """Raised when budget inputs are missing or invalid."""


@dataclass(frozen=True)
class BudgetSettings:
    name: str
    limit_usd: Decimal
    recipients: tuple[str, ...]

    def __post_init__(self) -> None:
        problems = []
        if not _BUDGET_NAME.fullmatch(self.name) or not self.name.strip():
            problems.append("budget name must be 1-100 characters without ':' or '\\'")
        if not self.limit_usd.is_finite() or self.limit_usd <= 0:
            problems.append("budget amount must be a positive number of USD")
        if not self.recipients:
            problems.append("at least one alert recipient is required")
        if len(self.recipients) > MAX_RECIPIENTS:
            problems.append(f"at most {MAX_RECIPIENTS} alert recipients are allowed")
        # Recipient addresses are private; report positions, never values.
        problems.extend(
            f"recipient {position} is not a valid email address"
            for position, address in enumerate(self.recipients, start=1)
            if not _EMAIL.fullmatch(address)
        )
        if problems:
            raise BudgetSettingsError("; ".join(problems))


def settings_from_environment(environ: Mapping[str, str]) -> BudgetSettings:
    required = (ENV_BUDGET_NAME, ENV_BUDGET_LIMIT_USD, ENV_BUDGET_RECIPIENTS)
    missing = [name for name in required if not environ.get(name, "").strip()]
    if missing:
        raise BudgetSettingsError(
            f"Missing budget inputs: {', '.join(missing)}. "
            "No budget amount or recipient is inferred."
        )
    try:
        limit_usd = Decimal(environ[ENV_BUDGET_LIMIT_USD].strip())
    except InvalidOperation:
        raise BudgetSettingsError(
            f"{ENV_BUDGET_LIMIT_USD}: budget amount must be a positive number of USD"
        ) from None
    recipients = tuple(
        part.strip() for part in environ[ENV_BUDGET_RECIPIENTS].split(",") if part.strip()
    )
    return BudgetSettings(
        name=environ[ENV_BUDGET_NAME].strip(), limit_usd=limit_usd, recipients=recipients
    )


class BudgetStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, settings: BudgetSettings, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        subscribers = [
            budgets.CfnBudget.SubscriberProperty(subscription_type="EMAIL", address=address)
            for address in settings.recipients
        ]
        budgets.CfnBudget(
            self,
            "MonthlyCostBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=settings.name,
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=float(settings.limit_usd), unit="USD"
                ),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="ACTUAL",
                        comparison_operator="GREATER_THAN",
                        threshold=threshold,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=subscribers,
                )
                for threshold in ALERT_THRESHOLDS_PERCENT
            ],
        )
