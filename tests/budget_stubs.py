"""Stubbed AWS Budgets and STS clients for offline tests.

botocore's Stubber validates every request and response against the real AWS
service models, so the tests exercise the real API shapes without any network
call or credential. A stub never counts as live budget evidence (ADR-003).
"""

from typing import Any

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_budgets import BudgetsClient
from mypy_boto3_sts import STSClient

# AWS documentation placeholder account, not a real account.
ACCOUNT_ID = "123456789012"
BUDGET_NAME = "aera-test-monthly"
EMAIL_SUBSCRIBER = {"SubscriptionType": "EMAIL", "Address": "alerts@example.com"}

_UNSIGNED = Config(signature_version=UNSIGNED)


def budgets_client() -> BudgetsClient:
    return boto3.client("budgets", region_name="us-east-1", config=_UNSIGNED)


def sts_client() -> STSClient:
    return boto3.client("sts", region_name="us-east-1", config=_UNSIGNED)


def compliant_budget(**overrides: Any) -> dict[str, Any]:
    budget: dict[str, Any] = {
        "BudgetName": BUDGET_NAME,
        "BudgetLimit": {"Amount": "100.0", "Unit": "USD"},
        "TimeUnit": "MONTHLY",
        "BudgetType": "COST",
    }
    budget.update(overrides)
    return budget


def actual_alert(threshold: float, **overrides: Any) -> dict[str, Any]:
    alert: dict[str, Any] = {
        "NotificationType": "ACTUAL",
        "ComparisonOperator": "GREATER_THAN",
        "Threshold": threshold,
        "ThresholdType": "PERCENTAGE",
    }
    alert.update(overrides)
    return alert


def stub_budget(stubber: Stubber, budget: dict[str, Any]) -> None:
    stubber.add_response(
        "describe_budget",
        {"Budget": budget},
        {"AccountId": ACCOUNT_ID, "BudgetName": BUDGET_NAME},
    )


def stub_notifications(stubber: Stubber, notifications: list[dict[str, Any]]) -> None:
    stubber.add_response(
        "describe_notifications_for_budget",
        {"Notifications": notifications},
        {"AccountId": ACCOUNT_ID, "BudgetName": BUDGET_NAME},
    )


def stub_subscribers(
    stubber: Stubber, notification: dict[str, Any], subscribers: list[dict[str, Any]]
) -> None:
    # The service model forbids an empty list; the API omits the key instead.
    stubber.add_response(
        "describe_subscribers_for_notification",
        {"Subscribers": subscribers} if subscribers else {},
        {"AccountId": ACCOUNT_ID, "BudgetName": BUDGET_NAME, "Notification": notification},
    )


def stub_compliant_account(stubber: Stubber) -> None:
    stub_budget(stubber, compliant_budget())
    alerts = [actual_alert(threshold) for threshold in (50.0, 80.0, 100.0)]
    stub_notifications(stubber, alerts)
    for alert in alerts:
        stub_subscribers(stubber, alert, [EMAIL_SUBSCRIBER])


def stub_caller_identity(stubber: Stubber) -> None:
    stubber.add_response(
        "get_caller_identity",
        {
            "UserId": "AIDATESTUSER",
            "Account": ACCOUNT_ID,
            "Arn": f"arn:aws:iam::{ACCOUNT_ID}:user/test",
        },
        {},
    )
