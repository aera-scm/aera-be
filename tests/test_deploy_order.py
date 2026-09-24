"""The budget gate runs before every bootstrap and deployment (NFR-COST-01, C-02).

Each test records the order of budget verification and CDK commands. A failed
verification must leave the command list empty: nothing may reach AWS
CloudFormation or ``cdk bootstrap`` without a verified budget.
"""

import subprocess
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from botocore.stub import Stubber
from budget_stubs import (
    BUDGET_NAME,
    budgets_client,
    sts_client,
    stub_caller_identity,
    stub_compliant_account,
)
from check_budget import BudgetCheckError, BudgetReport
from deploy_dev import DEPLOYABLE_ENVIRONMENTS, DeploymentRefusedError, main, run

from infra import environments
from infra.budget_app import budget_stack_name

REPORT = BudgetReport(
    name=BUDGET_NAME,
    limit_usd=Decimal("100"),
    subscribers_per_threshold={50: 1, 80: 1, 100: 1},
)


def test_oidc_bootstrap_requires_scoped_customer_policy() -> None:
    recorder = Recorder()
    with pytest.raises(DeploymentRefusedError):
        run(
            "bootstrap",
            env_name="dev",
            profile="synthetic",
            region="us-east-1",
            verify=recorder.verify_ok,
            runner=recorder.runner,
            environ={"AERA_GITHUB_REPOSITORY": "example/backend", "AERA_CDK_QUALIFIER": "aeradev"},
        )
    assert recorder.events == []


def test_oidc_bootstrap_uses_approved_qualifier_and_policy_after_budget() -> None:
    recorder = Recorder()
    policy = "arn:aws:iam::123456789012:policy/synthetic-dev-foundations"
    run(
        "bootstrap",
        env_name="dev",
        profile="synthetic",
        region="us-east-1",
        verify=recorder.verify_ok,
        runner=recorder.runner,
        environ={"AERA_CDK_QUALIFIER": "aeradev", "AERA_CFN_EXECUTION_POLICY_ARN": policy},
    )
    assert recorder.events == ["verify", "bootstrap"]
    assert recorder.commands[0][-4:] == (
        "--qualifier",
        "aeradev",
        "--cloudformation-execution-policies",
        policy,
    )


def label(command: Sequence[str]) -> str:
    if "bootstrap" in command:
        return "bootstrap"
    if "--all" in command:
        return "deploy-all"
    if budget_stack_name("dev") in command:
        return "deploy-budget"
    raise AssertionError(f"unexpected command {command}")


class Recorder:
    def __init__(self, fail_on: str | None = None) -> None:
        self.events: list[str] = []
        self.commands: list[tuple[str, ...]] = []
        self.environments: list[Mapping[str, str]] = []
        self.fail_on = fail_on

    def verify_ok(self) -> BudgetReport:
        self.events.append("verify")
        return REPORT

    def verify_fails(self) -> BudgetReport:
        self.events.append("verify")
        raise BudgetCheckError(["no actual-spend alert at 80%"])

    def runner(self, command: Sequence[str], environment: Mapping[str, str]) -> None:
        self.events.append(label(command))
        self.commands.append(tuple(command))
        self.environments.append(dict(environment))
        if label(command) == self.fail_on:
            raise subprocess.CalledProcessError(3, list(command))


BUNDLE = str(Path(__file__).parent / "fixtures" / "lambda-bundle")
OWNER = {"AERA_OWNER_TAG": "aera-test-owner", "AERA_LAMBDA_BUNDLE": BUNDLE}


def run_action(action: str, recorder: Recorder, *, verified: bool = True, env: str = "dev") -> Any:
    return run(
        action,
        env_name=env,
        profile="aera-test",
        region="us-east-1",
        verify=recorder.verify_ok if verified else recorder.verify_fails,
        runner=recorder.runner,
        environ=OWNER,
    )


def test_nfr_cost_01_deploy_verifies_budget_then_bootstraps_then_deploys() -> None:
    recorder = Recorder()

    report = run_action("deploy", recorder)

    assert recorder.events == ["verify", "bootstrap", "deploy-all"]
    assert report == REPORT


def test_nfr_cost_01_bootstrap_verifies_budget_first() -> None:
    recorder = Recorder()

    run_action("bootstrap", recorder)

    assert recorder.events == ["verify", "bootstrap"]


@pytest.mark.parametrize("action", ["bootstrap", "deploy"])
def test_nfr_cost_01_failed_verification_blocks_every_later_call(action: str) -> None:
    recorder = Recorder()

    with pytest.raises(BudgetCheckError):
        run_action(action, recorder, verified=False)

    assert recorder.events == ["verify"]
    assert recorder.commands == []


def test_nfr_cost_01_budget_action_creates_budget_stack_then_verifies() -> None:
    recorder = Recorder()

    run_action("budget", recorder)

    assert recorder.events == ["deploy-budget", "verify"]
    command = recorder.commands[0]
    assert command[-1] == "aera-dev-budget"
    assert "uv run --locked python -m infra.budget_app" in command
    assert "bootstrap" not in command


def test_nfr_cost_01_budget_action_reports_failed_verification() -> None:
    recorder = Recorder()

    with pytest.raises(BudgetCheckError):
        run_action("budget", recorder, verified=False)

    assert recorder.events == ["deploy-budget", "verify"]


def test_failed_bootstrap_stops_deployment() -> None:
    recorder = Recorder(fail_on="bootstrap")

    with pytest.raises(subprocess.CalledProcessError):
        run_action("deploy", recorder)

    assert recorder.events == ["verify", "bootstrap"]


def test_commands_target_the_requested_profile_region_and_environment() -> None:
    recorder = Recorder()

    run_action("deploy", recorder)

    for command, environment in zip(recorder.commands, recorder.environments, strict=True):
        assert command[:3] == ("pnpm", "exec", "cdk")
        assert command[command.index("--profile") + 1] == "aera-test"
        assert environment == {"AERA_ENV": "dev", "AWS_REGION": "us-east-1"}


@pytest.mark.parametrize("action", ["budget", "bootstrap", "deploy"])
@pytest.mark.parametrize("env", ["final", "prod", ""])
def test_final_and_unknown_environments_are_refused(action: str, env: str) -> None:
    recorder = Recorder()

    with pytest.raises(DeploymentRefusedError):
        run_action(action, recorder, env=env)

    assert recorder.events == []


@pytest.mark.parametrize("action", ["budget", "bootstrap", "deploy"])
def test_nfr_cmp_02_unapproved_region_is_refused_before_any_call(action: str) -> None:
    recorder = Recorder()

    with pytest.raises(DeploymentRefusedError, match="not the approved region"):
        run(
            action,
            env_name="dev",
            profile="aera-test",
            region="eu-west-1",
            verify=recorder.verify_ok,
            runner=recorder.runner,
        )

    assert recorder.events == []


@pytest.mark.parametrize(
    "models",
    [
        {
            "MODEL_SUPERVISOR_ID": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "MODEL_SMALL_ID": "amazon.nova-lite-v1:0",
        },
        {"MODEL_SUPERVISOR_ID": "anthropic.claude-sonnet-4-5-20250929-v1:0"},
    ],
)
def test_a_01_deploy_refuses_invalid_model_ids_before_any_call(models: dict[str, str]) -> None:
    recorder = Recorder()

    with pytest.raises(DeploymentRefusedError, match="MODEL_"):
        run(
            "deploy",
            env_name="dev",
            profile="aera-test",
            region="us-east-1",
            verify=recorder.verify_ok,
            runner=recorder.runner,
            environ=models,
        )

    assert recorder.events == []


def test_a_01_deploy_accepts_valid_model_ids() -> None:
    recorder = Recorder()

    run(
        "deploy",
        env_name="dev",
        profile="aera-test",
        region="us-east-1",
        verify=recorder.verify_ok,
        runner=recorder.runner,
        environ={
            **OWNER,
            "MODEL_SUPERVISOR_ID": "anthropic.claude-sonnet-4-5-20250929-v1:0",
            "MODEL_SMALL_ID": "amazon.nova-lite-v1:0",
        },
    )

    assert recorder.events == ["verify", "bootstrap", "deploy-all"]


@pytest.mark.parametrize("owner", [None, "", "   "])
def test_srd_6_16_deploy_refuses_missing_owner_tag_before_any_call(owner: str | None) -> None:
    recorder = Recorder()
    environ = {} if owner is None else {"AERA_OWNER_TAG": owner}

    with pytest.raises(DeploymentRefusedError, match="AERA_OWNER_TAG"):
        run(
            "deploy",
            env_name="dev",
            profile="aera-test",
            region="us-east-1",
            verify=recorder.verify_ok,
            runner=recorder.runner,
            environ=environ,
        )

    assert recorder.events == []


def test_srd_6_16_bootstrap_does_not_need_the_owner_tag() -> None:
    recorder = Recorder()

    run(
        "bootstrap",
        env_name="dev",
        profile="aera-test",
        region="us-east-1",
        verify=recorder.verify_ok,
        runner=recorder.runner,
        environ={},
    )

    assert recorder.events == ["verify", "bootstrap"]


def test_unknown_action_is_refused() -> None:
    recorder = Recorder()

    with pytest.raises(DeploymentRefusedError):
        run_action("destroy", recorder)

    assert recorder.events == []


def test_deploy_script_and_budget_app_agree_on_deployable_environments() -> None:
    assert DEPLOYABLE_ENVIRONMENTS == environments.DEPLOYABLE_ENVIRONMENTS == {"dev"}


def no_aws(profile: str, region: str) -> Any:
    raise AssertionError("AWS must not be contacted")


def no_commands(command: Sequence[str], environment: Mapping[str, str]) -> None:
    raise AssertionError("no command may run")


def test_cli_refuses_final_before_contacting_aws(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["deploy", "--env", "final", "--profile", "aera-test", "--budget-name", BUDGET_NAME],
        clients_factory=no_aws,
        runner=no_commands,
    )

    assert code == 1
    assert "final" in capsys.readouterr().err


def test_cli_missing_budget_blocks_bootstrap_through_real_verification(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AERA_OWNER_TAG", "aera-test-owner")
    monkeypatch.setenv("AERA_LAMBDA_BUNDLE", BUNDLE)
    sts, budgets = sts_client(), budgets_client()
    with Stubber(sts) as sts_stub, Stubber(budgets) as budgets_stub:
        stub_caller_identity(sts_stub)
        budgets_stub.add_client_error("describe_budget", service_error_code="NotFoundException")

        code = main(
            ["deploy", "--env", "dev", "--profile", "aera-test", "--budget-name", BUDGET_NAME],
            clients_factory=lambda profile, region: (sts, budgets),
            runner=no_commands,
        )

    assert code == 1
    assert "was not found" in capsys.readouterr().err


def test_cli_verified_budget_allows_bootstrap_and_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AERA_OWNER_TAG", "aera-test-owner")
    monkeypatch.setenv("AERA_LAMBDA_BUNDLE", BUNDLE)
    recorder = Recorder()
    sts, budgets = sts_client(), budgets_client()
    with Stubber(sts) as sts_stub, Stubber(budgets) as budgets_stub:
        stub_caller_identity(sts_stub)
        stub_compliant_account(budgets_stub)

        code = main(
            ["deploy", "--env", "dev", "--profile", "aera-test", "--budget-name", BUDGET_NAME],
            clients_factory=lambda profile, region: (sts, budgets),
            runner=recorder.runner,
        )
        budgets_stub.assert_no_pending_responses()

    assert code == 0
    assert recorder.events == ["bootstrap", "deploy-all"]


def test_cli_returns_failing_command_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AERA_OWNER_TAG", "aera-test-owner")
    monkeypatch.setenv("AERA_LAMBDA_BUNDLE", BUNDLE)
    recorder = Recorder(fail_on="bootstrap")
    sts, budgets = sts_client(), budgets_client()
    with Stubber(sts) as sts_stub, Stubber(budgets) as budgets_stub:
        stub_caller_identity(sts_stub)
        stub_compliant_account(budgets_stub)

        code = main(
            ["deploy", "--env", "dev", "--profile", "aera-test", "--budget-name", BUDGET_NAME],
            clients_factory=lambda profile, region: (sts, budgets),
            runner=recorder.runner,
        )

    assert code == 3
    assert recorder.events == ["bootstrap"]


@pytest.mark.parametrize("bundle", [None, "", "does/not/exist"])
def test_deploy_refuses_without_a_built_lambda_bundle(bundle: str | None) -> None:
    recorder = Recorder()
    environ = {"AERA_OWNER_TAG": "aera-test-owner"}
    if bundle is not None:
        environ["AERA_LAMBDA_BUNDLE"] = bundle

    with pytest.raises(DeploymentRefusedError, match="bundle|build_lambda"):
        run(
            "deploy",
            env_name="dev",
            profile="aera-test",
            region="us-east-1",
            verify=recorder.verify_ok,
            runner=recorder.runner,
            environ=environ,
        )
    assert recorder.events == []
