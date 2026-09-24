"""Budget-gated budget creation, bootstrap and deployment (NFR-COST-01, C-02, NFR-MNT-02).

The order is fixed. ``budget`` deploys the standalone budget stack and then
verifies it. ``bootstrap`` and ``deploy`` verify the budget first; a failed
verification stops before any bootstrap or CloudFormation call. Only ``dev`` is
accepted; ``final`` is deployed from a tagged release, never from here.

    uv run --locked python scripts/deploy_dev.py {budget,bootstrap,deploy} --env dev
"""

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from check_budget import (
    BudgetCheckError,
    BudgetReport,
    ClientsFactory,
    add_budget_arguments,
    open_clients,
    require_budget_arguments,
    verify_with_clients,
)
from check_model_access import SMALL_VARIABLE, SUPERVISOR_VARIABLE, model_id_problems
from check_region import region_problems

ROOT = Path(__file__).resolve().parents[1]

# Mirrors infra.environments.DEPLOYABLE_ENVIRONMENTS; a test keeps them equal.
DEPLOYABLE_ENVIRONMENTS: frozenset[str] = frozenset({"dev"})
ACTIONS = ("budget", "bootstrap", "deploy")
CDK = ("pnpm", "exec", "cdk")
BUDGET_APP = "uv run --locked python -m infra.budget_app"
MAIN_APP = "uv run --locked python -m infra.app"
MODEL_VARIABLES = (SUPERVISOR_VARIABLE, SMALL_VARIABLE)

Runner = Callable[[Sequence[str], Mapping[str, str]], None]
Verifier = Callable[[], BudgetReport]


class DeploymentRefusedError(Exception):
    """Raised for an environment or action this script must not run."""


def run_command(command: Sequence[str], environment: Mapping[str, str]) -> None:
    subprocess.run(list(command), cwd=ROOT, env={**os.environ, **environment}, check=True)


def run(
    action: str,
    *,
    env_name: str,
    profile: str,
    region: str,
    verify: Verifier,
    runner: Runner,
    environ: Mapping[str, str] | None = None,
) -> BudgetReport:
    if env_name not in DEPLOYABLE_ENVIRONMENTS:
        raise DeploymentRefusedError(
            f"Environment {env_name!r} cannot be deployed from here; only 'dev' is allowed."
        )
    if action not in ACTIONS:
        raise DeploymentRefusedError(f"Unknown action {action!r}; expected one of {ACTIONS}.")
    # NFR-CMP-02: child processes get AWS_REGION from here, so only the region matters.
    problems = region_problems(region, {})
    if problems:
        raise DeploymentRefusedError("; ".join(problems))
    if action == "deploy":
        # A-01: approved model ids reach SSM through the data stack; validate them first.
        models = [(environ or {}).get(name, "").strip() for name in MODEL_VARIABLES]
        if any(models):
            problems = model_id_problems(*models)
            if problems:
                raise DeploymentRefusedError("; ".join(problems))
        # SRD 6.16: the main app tags every resource with the owner; fail before any call.
        if not (environ or {}).get("AERA_OWNER_TAG", "").strip():
            raise DeploymentRefusedError(
                "AERA_OWNER_TAG is required: the owner tag on every resource (SRD 6.16)."
            )
    environment = {"AERA_ENV": env_name, "AWS_REGION": region}
    profile_args = ("--profile", profile)
    bootstrap_args: tuple[str, ...] = ()
    inputs = environ or {}
    qualifier = inputs.get("AERA_CDK_QUALIFIER", "")
    if inputs.get("AERA_GITHUB_REPOSITORY") or qualifier:
        execution_policy = inputs.get("AERA_CFN_EXECUTION_POLICY_ARN", "")
        if not re.fullmatch(r"[a-zA-Z0-9]{1,10}", qualifier) or not re.fullmatch(
            r"arn:aws:iam::[0-9]{12}:policy/[A-Za-z0-9+=,.@_/-]+", execution_policy
        ):
            raise DeploymentRefusedError(
                "OIDC bootstrap needs an approved qualifier and scoped execution policy."
            )
        bootstrap_args = (
            "--qualifier",
            qualifier,
            "--cloudformation-execution-policies",
            execution_policy,
        )

    if action == "budget":
        stack = f"aera-{env_name}-budget"
        runner((*CDK, "deploy", "--app", BUDGET_APP, *profile_args, stack), environment)
        return verify()

    report = verify()
    runner((*CDK, "bootstrap", *profile_args, *bootstrap_args), environment)
    if action == "deploy":
        runner((*CDK, "deploy", "--all", "--app", MAIN_APP, *profile_args), environment)
    return report


def main(
    argv: Sequence[str] | None = None,
    *,
    clients_factory: ClientsFactory = open_clients,
    runner: Runner = run_command,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--env", required=True)
    add_budget_arguments(parser)
    args = parser.parse_args(argv)
    require_budget_arguments(parser, args)

    def verify() -> BudgetReport:
        return verify_with_clients(
            *clients_factory(args.profile, args.region),
            budget_name=args.budget_name,
            expected_limit_usd=args.expected_limit_usd,
        )

    try:
        report = run(
            args.action,
            env_name=args.env,
            profile=args.profile,
            region=args.region,
            verify=verify,
            runner=runner,
            environ=os.environ,
        )
    except (DeploymentRefusedError, BudgetCheckError) as error:
        print(error, file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(
            f"Command failed with exit code {error.returncode}; later steps were not run.",
            file=sys.stderr,
        )
        return error.returncode or 1
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
