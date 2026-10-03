"""Register the deployed SAP Mirror with AERA (SRD 6.16, IR-02, IR-03, NFR-SEC-03).

Writes the Mirror's URL to SSM as SAP_READ_BASE and SAP_WRITE_BASE and places its OAuth
client (from `cf service-key`) into the Mirror secret container. The client is read from
MIRROR_CLIENT_ID, MIRROR_CLIENT_SECRET and MIRROR_TOKEN_URL in this process's environment
only; it is never accepted as an argument and never printed.

    uv run --locked python scripts/register_mirror.py --env dev --url https://<mirror-host>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from check_region import APPROVED_REGION, client_region_problems, error_code, region_problems
from deploy_dev import DEPLOYABLE_ENVIRONMENTS

if TYPE_CHECKING:
    from mypy_boto3_secretsmanager import SecretsManagerClient
    from mypy_boto3_ssm import SSMClient

CLIENT_VARIABLES = ("MIRROR_CLIENT_ID", "MIRROR_CLIENT_SECRET", "MIRROR_TOKEN_URL")
Clients = tuple["SSMClient", "SecretsManagerClient"]


def https_origin(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.path in ("", "/")
        and not parts.query
        and not parts.fragment
        and not parts.username
    )


def open_clients(profile: str, region: str) -> Clients:
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("ssm"), session.client("secretsmanager")


def main(
    argv: Sequence[str] | None = None,
    *,
    clients_factory: Callable[[str, str], Clients] = open_clients,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--env", required=True)
    parser.add_argument("--url", required=True, help="Mirror origin, e.g. https://<host>")
    parser.add_argument("--profile", default=os.environ.get("AERA_AWS_PROFILE"))
    parser.add_argument("--region", default=os.environ.get("AERA_REGION", APPROVED_REGION))
    parser.add_argument("--secret-name", default=os.environ.get("AERA_SAP_MIRROR_SECRET_NAME"))
    args = parser.parse_args(argv)
    if not args.profile:
        parser.error("--profile or AERA_AWS_PROFILE is required")

    if args.env not in DEPLOYABLE_ENVIRONMENTS:
        print(f"Environment {args.env!r} refused; only 'dev' is allowed.", file=sys.stderr)
        return 1
    if not https_origin(args.url):
        print("The Mirror URL must be a plain https origin.", file=sys.stderr)
        return 1
    for name in CLIENT_VARIABLES:
        if not os.environ.get(name, "").strip():
            print(f"{name} is not set in this process environment.", file=sys.stderr)
            return 1
    problems = region_problems(args.region, os.environ)
    if problems:
        print("; ".join(problems), file=sys.stderr)
        return 1

    ssm, secrets = clients_factory(args.profile, args.region)
    mismatched = client_region_problems([ssm, secrets], args.region)
    if mismatched:
        print("; ".join(mismatched), file=sys.stderr)
        return 1
    url = args.url.rstrip("/")
    secret_name = args.secret_name or f"/aera/{args.env}/sap/mirror-oauth-client"
    operation = "PutParameter"
    try:
        for key in ("SAP_READ_BASE", "SAP_WRITE_BASE"):
            ssm.put_parameter(
                Name=f"/aera/{args.env}/{key}", Value=url, Type="String", Overwrite=True
            )
        operation = "PutSecretValue"
        client = {
            "clientId": os.environ["MIRROR_CLIENT_ID"].strip(),
            "clientSecret": os.environ["MIRROR_CLIENT_SECRET"].strip(),
            "tokenUrl": os.environ["MIRROR_TOKEN_URL"].strip(),
        }
        secrets.put_secret_value(
            SecretId=secret_name, SecretString=json.dumps(client, sort_keys=True)
        )
    except (ClientError, BotoCoreError) as error:
        print(f"{operation} failed with {error_code(error)}.", file=sys.stderr)
        return 1
    print(f"SAP_READ_BASE and SAP_WRITE_BASE set to {url}; Mirror client stored in {secret_name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
