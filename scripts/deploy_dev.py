"""Budget-gated budget creation, bootstrap and deployment (NFR-COST-01, C-02, NFR-MNT-02).

The order is fixed. ``budget`` deploys the standalone budget stack and then
verifies it. ``bootstrap`` and ``deploy`` verify the budget first; a failed
verification stops before any bootstrap or CloudFormation call. Only ``dev`` is
accepted; ``final`` is deployed from a tagged release, never from here.

    uv run --locked python scripts/deploy_dev.py {budget,bootstrap,deploy} --env dev
"""

import argparse
import os
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

ROOT = Path(__file__).resolve().parents[1]

# Mirrors infra.environments.DEPLOYABLE_ENVIRONMENTS; a test keeps them equal.
DEPLOYABLE_ENVIRONMENTS: frozenset[str] = frozenset({"dev"})
ACTIONS = ("budget", "bootstrap", "deploy")
CDK = ("pnpm", "exec", "cdk")
BUDGET_APP = "uv run --locked python -m infra.budget_app"
MAIN_APP = "uv run --locked python -m infra.app"

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
) -> BudgetReport:
    if env_name not in DEPLOYABLE_ENVIRONMENTS:
        raise DeploymentRefusedError(
            f"Environment {env_name!r} cannot be deployed from here; only 'dev' is allowed."
        )
    if action not in ACTIONS:
        raise DeploymentRefusedError(f"Unknown action {action!r}; expected one of {ACTIONS}.")
    environment = {"AERA_ENV": env_name, "AWS_REGION": region}
    profile_args = ("--profile", profile)

    if action == "budget":
        stack = f"aera-{env_name}-budget"
        runner((*CDK, "deploy", "--app", BUDGET_APP, *profile_args, stack), environment)
        return verify()

    report = verify()
    runner((*CDK, "bootstrap", *profile_args), environment)
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
