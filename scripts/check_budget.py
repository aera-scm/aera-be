"""Verify the AWS budget gate before any other deployment (NFR-COST-01, C-02).

Reads the budget through the AWS Budgets API and checks a monthly USD cost
budget with actual-spend alerts at 50, 80 and 100 percent, each with at least
one subscriber. Output reports the amount, thresholds and subscriber counts; it
never prints recipient addresses or the account id.

    uv run --locked python scripts/check_budget.py --profile <name> --budget-name <name>
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_budgets import BudgetsClient
    from mypy_boto3_sts import STSClient

REQUIRED_THRESHOLDS_PERCENT: tuple[int, ...] = (50, 80, 100)
DEFAULT_REGION = "us-east-1"

ClientsFactory = Callable[[str, str], "tuple[STSClient, BudgetsClient]"]


class BudgetCheckError(Exception):
    """Raised when the budget is missing, misconfigured or cannot be read."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("Budget verification failed: " + "; ".join(self.problems))


@dataclass(frozen=True)
class BudgetReport:
    name: str
    limit_usd: Decimal
    subscribers_per_threshold: Mapping[int, int]

    def summary(self) -> str:
        alerts = ", ".join(
            f"{threshold}% ({count} subscriber{'' if count == 1 else 's'})"
            for threshold, count in self.subscribers_per_threshold.items()
        )
        return (
            f"Budget '{self.name}' verified: monthly USD {self.limit_usd} cost budget; "
            f"actual-spend alerts at {alerts}."
        )


def _api_failure(operation: str, error: ClientError | BotoCoreError, budget_name: str) -> str:
    # Only the error code is reported; AWS messages can contain the account id.
    if isinstance(error, BotoCoreError):
        return f"Budgets API {operation} failed with {type(error).__name__}"
    code = error.response.get("Error", {}).get("Code", "Unknown")
    if code == "NotFoundException":
        return f"budget '{budget_name}' was not found"
    return f"Budgets API {operation} failed with {code}"


def _is_actual_percentage_alert(notification: Mapping[str, Any], threshold: int) -> bool:
    return (
        notification.get("NotificationType") == "ACTUAL"
        and notification.get("ComparisonOperator") == "GREATER_THAN"
        # The Budgets API documents PERCENTAGE as the default threshold type.
        and notification.get("ThresholdType", "PERCENTAGE") == "PERCENTAGE"
        and Decimal(str(notification.get("Threshold"))) == threshold
    )


def _limit_problems(
    budget: Mapping[str, Any], expected_limit_usd: Decimal | None
) -> tuple[Decimal | None, list[str]]:
    limit = budget.get("BudgetLimit")
    if limit is None:
        return None, ["budget has no fixed spend limit"]
    problems = []
    if limit.get("Unit") != "USD":
        problems.append(f"budget limit unit is {limit.get('Unit')!r}, expected 'USD'")
    try:
        amount = Decimal(limit.get("Amount", ""))
    except InvalidOperation:
        return None, [*problems, "budget limit amount is not a number"]
    if not amount.is_finite() or amount <= 0:
        problems.append("budget limit amount must be positive")
    elif expected_limit_usd is not None and amount != expected_limit_usd:
        problems.append(f"budget limit is USD {amount}, expected USD {expected_limit_usd}")
    return amount, problems


def verify_budget(
    client: BudgetsClient,
    *,
    account_id: str,
    budget_name: str,
    expected_limit_usd: Decimal | None = None,
) -> BudgetReport:
    try:
        budget = client.describe_budget(AccountId=account_id, BudgetName=budget_name)["Budget"]
    except (ClientError, BotoCoreError) as error:
        raise BudgetCheckError([_api_failure("DescribeBudget", error, budget_name)]) from None

    problems = []
    if budget.get("BudgetType") != "COST":
        problems.append(f"budget type is {budget.get('BudgetType')!r}, expected 'COST'")
    if budget.get("TimeUnit") != "MONTHLY":
        problems.append(f"budget period is {budget.get('TimeUnit')!r}, expected 'MONTHLY'")
    amount, limit_problems = _limit_problems(budget, expected_limit_usd)
    problems.extend(limit_problems)

    subscribers: dict[int, int] = {}
    operation = "DescribeNotificationsForBudget"
    try:
        notifications = [
            notification
            for page in client.get_paginator("describe_notifications_for_budget").paginate(
                AccountId=account_id, BudgetName=budget_name
            )
            for notification in page.get("Notifications", [])
        ]
        operation = "DescribeSubscribersForNotification"
        for threshold in REQUIRED_THRESHOLDS_PERCENT:
            matching = [n for n in notifications if _is_actual_percentage_alert(n, threshold)]
            if not matching:
                problems.append(f"no actual-spend alert at {threshold}%")
                continue
            subscribers[threshold] = sum(
                len(page.get("Subscribers", []))
                for notification in matching
                for page in client.get_paginator("describe_subscribers_for_notification").paginate(
                    AccountId=account_id, BudgetName=budget_name, Notification=notification
                )
            )
            if subscribers[threshold] == 0:
                problems.append(f"alert at {threshold}% has no subscribers")
    except (ClientError, BotoCoreError) as error:
        raise BudgetCheckError([*problems, _api_failure(operation, error, budget_name)]) from None

    if problems or amount is None:
        raise BudgetCheckError(problems)
    return BudgetReport(name=budget_name, limit_usd=amount, subscribers_per_threshold=subscribers)


def verify_with_clients(
    sts: STSClient,
    budgets: BudgetsClient,
    *,
    budget_name: str,
    expected_limit_usd: Decimal | None = None,
) -> BudgetReport:
    try:
        account_id = sts.get_caller_identity()["Account"]
    except (ClientError, BotoCoreError) as error:
        name = type(error).__name__
        if isinstance(error, ClientError):
            name = error.response.get("Error", {}).get("Code", name)
        raise BudgetCheckError([f"cannot resolve the AWS account: {name}"]) from None
    return verify_budget(
        budgets,
        account_id=account_id,
        budget_name=budget_name,
        expected_limit_usd=expected_limit_usd,
    )


def open_clients(profile: str, region: str) -> tuple[STSClient, BudgetsClient]:
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("sts"), session.client("budgets")


def parse_usd(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a USD amount: {text!r}") from None


def add_budget_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", default=os.environ.get("AERA_AWS_PROFILE"))
    parser.add_argument("--region", default=os.environ.get("AERA_REGION", DEFAULT_REGION))
    parser.add_argument("--budget-name", default=os.environ.get("AERA_BUDGET_NAME"))
    parser.add_argument(
        "--expected-limit-usd",
        type=parse_usd,
        default=os.environ.get("AERA_BUDGET_LIMIT_USD"),
        help="approved monthly amount; defaults to AERA_BUDGET_LIMIT_USD",
    )


def require_budget_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.profile:
        parser.error("--profile or AERA_AWS_PROFILE is required")
    if not args.budget_name:
        parser.error("--budget-name or AERA_BUDGET_NAME is required")


def main(
    argv: Sequence[str] | None = None, *, clients_factory: ClientsFactory = open_clients
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    add_budget_arguments(parser)
    args = parser.parse_args(argv)
    require_budget_arguments(parser, args)
    try:
        report = verify_with_clients(
            *clients_factory(args.profile, args.region),
            budget_name=args.budget_name,
            expected_limit_usd=args.expected_limit_usd,
        )
    except BudgetCheckError as error:
        print(error, file=sys.stderr)
        return 1
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
