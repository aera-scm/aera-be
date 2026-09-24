"""Deploy already bootstrapped dev foundations after successful main CI (C-02)."""

import os
import re
import subprocess
import sys
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

import boto3
from check_budget import BudgetCheckError, BudgetReport, verify_with_clients
from check_model_access import model_id_problems
from check_region import region_problems
from deploy_dev import CDK, MAIN_APP, DeploymentRefusedError, Runner, Verifier, run_command


def validate(environ: Mapping[str, str]) -> Decimal:
    repository = environ.get("AERA_GITHUB_REPOSITORY", "")
    if (
        environ.get("AERA_ENV") != "dev"
        or region_problems(environ.get("AERA_REGION", ""), environ)
        or environ.get("GITHUB_REF") != "refs/heads/main"
        or environ.get("GITHUB_EVENT_NAME") != "workflow_run"
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or environ.get("GITHUB_REPOSITORY") != repository
        or not re.fullmatch(r"[a-zA-Z0-9]{1,10}", environ.get("AERA_CDK_QUALIFIER", ""))
        or environ.get("AERA_GITHUB_PROVIDER_MODE") not in {"create", "existing"}
        or not environ.get("AERA_OWNER_TAG", "").strip()
        or not environ.get("AERA_BUDGET_NAME", "").strip()
    ):
        raise DeploymentRefusedError("CI deployment requires approved dev/main configuration.")
    try:
        limit = Decimal(environ.get("AERA_BUDGET_LIMIT_USD", ""))
        if not limit.is_finite() or limit <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise DeploymentRefusedError("An approved positive budget limit is required.") from None
    models = [environ.get(name, "").strip() for name in ("MODEL_SUPERVISOR_ID", "MODEL_SMALL_ID")]
    if any(models) and model_id_problems(*models):
        raise DeploymentRefusedError("Invalid regional model configuration.")
    return limit


def run(environ: Mapping[str, str], *, verify: Verifier, runner: Runner) -> BudgetReport:
    validate(environ)
    report = verify()
    runner(
        (*CDK, "deploy", "--all", "--app", MAIN_APP, "--require-approval", "never"),
        {**environ, "AWS_REGION": environ["AERA_REGION"]},
    )
    return report


def main() -> int:
    try:
        expected_limit = validate(os.environ)

        def verify() -> BudgetReport:
            session = boto3.Session(region_name=os.environ["AERA_REGION"])
            return verify_with_clients(
                session.client("sts"),
                session.client("budgets"),
                budget_name=os.environ["AERA_BUDGET_NAME"],
                expected_limit_usd=expected_limit,
            )

        report = run(os.environ, verify=verify, runner=run_command)
    except (DeploymentRefusedError, BudgetCheckError) as error:
        print(error, file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        print("CI deployment failed.", file=sys.stderr)
        return 1
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
