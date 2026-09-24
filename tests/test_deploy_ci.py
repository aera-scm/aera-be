"""CI cannot bootstrap, bypass budget verification, or deploy final."""

from collections.abc import Mapping, Sequence
from decimal import Decimal

import pytest
from check_budget import BudgetCheckError, BudgetReport
from deploy_ci import run
from deploy_dev import DeploymentRefusedError

ENV = {
    "AERA_ENV": "dev",
    "AERA_REGION": "us-east-1",
    "AERA_OWNER_TAG": "synthetic-owner",
    "AERA_GITHUB_REPOSITORY": "example/backend",
    "GITHUB_REPOSITORY": "example/backend",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_EVENT_NAME": "workflow_run",
    "AERA_CDK_QUALIFIER": "aeradev",
    "AERA_GITHUB_PROVIDER_MODE": "existing",
    "AERA_BUDGET_NAME": "synthetic-budget",
    "AERA_BUDGET_LIMIT_USD": "100",
}


def test_budget_verified_before_ci_deploy_without_bootstrap() -> None:
    calls: list[str] = []

    def verify() -> BudgetReport:
        calls.append("verify")
        return BudgetReport("synthetic-budget", Decimal("100"), {50: 1, 80: 1, 100: 1})

    def runner(command: Sequence[str], environment: Mapping[str, str]) -> None:
        calls.append("deploy")
        assert "bootstrap" not in command and "--profile" not in command
        assert "--all" in command and "--require-approval" in command
        assert environment["AERA_ENV"] == "dev"

    run(ENV, verify=verify, runner=runner)
    assert calls == ["verify", "deploy"]


def test_ci_budget_failure_prevents_every_command() -> None:
    def verify() -> BudgetReport:
        raise BudgetCheckError(["missing budget"])

    def runner(command: Sequence[str], environment: Mapping[str, str]) -> None:
        pytest.fail("unverified budget must prevent deployment")

    with pytest.raises(BudgetCheckError):
        run(ENV, verify=verify, runner=runner)


@pytest.mark.parametrize(
    "field,value",
    [
        ("AERA_ENV", "final"),
        ("AERA_REGION", "eu-west-1"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REPOSITORY", "other/backend"),
        ("AERA_GITHUB_REPOSITORY", ""),
        ("AERA_CDK_QUALIFIER", ""),
        ("AERA_OWNER_TAG", ""),
        ("AERA_BUDGET_LIMIT_USD", ""),
    ],
)
def test_ci_invalid_configuration_prevents_budget_and_deploy(field: str, value: str) -> None:
    def verify() -> BudgetReport:
        pytest.fail("invalid configuration must fail before cloud access")

    with pytest.raises(DeploymentRefusedError):
        run({**ENV, field: value}, verify=verify, runner=lambda *args: None)
