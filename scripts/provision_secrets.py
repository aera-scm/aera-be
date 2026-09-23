"""Place the SAP sandbox API key into its Secrets Manager container (NFR-SEC-03, IR-01).

The key is read from the ``SAP_SANDBOX_API_KEY`` variable of this process only;
it is never accepted as an argument, read from a file, printed or returned.
The owner exports it privately in their own shell. The container must already
exist (data stack), or be an approved existing secret named with
``--secret-name`` / ``AERA_SAP_SANDBOX_SECRET_NAME``.

    uv run --locked python scripts/provision_secrets.py --env dev [--profile <name>]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from check_region import APPROVED_REGION, client_region_problems, error_code, region_problems
from deploy_dev import DEPLOYABLE_ENVIRONMENTS

if TYPE_CHECKING:
    from mypy_boto3_secretsmanager import SecretsManagerClient

KEY_VARIABLE = "SAP_SANDBOX_API_KEY"


def open_client(profile: str, region: str) -> SecretsManagerClient:
    return boto3.Session(profile_name=profile, region_name=region).client("secretsmanager")


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[str, str], SecretsManagerClient] = open_client,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--env", required=True)
    parser.add_argument("--profile", default=os.environ.get("AERA_AWS_PROFILE"))
    parser.add_argument("--region", default=os.environ.get("AERA_REGION", APPROVED_REGION))
    parser.add_argument("--secret-name", default=os.environ.get("AERA_SAP_SANDBOX_SECRET_NAME"))
    args = parser.parse_args(argv)
    if not args.profile:
        parser.error("--profile or AERA_AWS_PROFILE is required")

    if args.env not in DEPLOYABLE_ENVIRONMENTS:
        print(f"Environment {args.env!r} refused; only 'dev' is allowed.", file=sys.stderr)
        return 1
    if not os.environ.get(KEY_VARIABLE, "").strip():
        print(f"{KEY_VARIABLE} is not set in this process environment.", file=sys.stderr)
        return 1
    problems = region_problems(args.region, os.environ)
    if problems:
        print("; ".join(problems), file=sys.stderr)
        return 1

    name = args.secret_name or f"/aera/{args.env}/sap/sandbox-api-key"
    client = client_factory(args.profile, args.region)
    mismatched = client_region_problems([client], args.region)
    if mismatched:
        print("; ".join(mismatched), file=sys.stderr)
        return 1
    try:
        client.put_secret_value(SecretId=name, SecretString=os.environ[KEY_VARIABLE].strip())
    except (ClientError, BotoCoreError) as error:
        code = error_code(error)
        if code == "ResourceNotFoundException":
            print(
                f"Secret container {name} does not exist; deploy the data stack first.",
                file=sys.stderr,
            )
        else:
            print(f"PutSecretValue failed with {code}.", file=sys.stderr)
        return 1
    print(f"Secret {name} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
