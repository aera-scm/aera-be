"""Seed the SRD 6.23 Config-table defaults idempotently (DR-10, NFR-MNT-02).

Each default is written only if its key is absent, so re-running never
overwrites a value an administrator changed. Only ``dev`` in the approved
region is accepted.

    uv run --locked python scripts/seed_config.py --env dev [--profile <name>]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from check_region import APPROVED_REGION, error_code, region_problems

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

# Run as a script, the repository root is not on the path; the defaults live in infra.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infra.config_defaults import CONFIG_DEFAULTS  # noqa: E402
from infra.environments import (  # noqa: E402
    EnvironmentRefusedError,
    require_deployable_environment,
)

SEED_ACTOR = "seed"


class SeedRefusedError(Exception):
    """Raised when seeding must not run or cannot complete."""


@dataclass(frozen=True)
class SeedResult:
    written: list[str]
    preserved: list[str]


def _attribute(value: Decimal | str) -> dict[str, str]:
    return {"N": str(value)} if isinstance(value, Decimal) else {"S": value}


def config_items(changed_at: str) -> list[dict[str, Any]]:
    return [
        {
            "PK": {"S": f"CFG#{key}"},
            "key": {"S": key},
            "value": _attribute(CONFIG_DEFAULTS[key]),
            "changedBy": {"S": SEED_ACTOR},
            "changedAt": {"S": changed_at},
        }
        for key in sorted(CONFIG_DEFAULTS)
    ]


def seed(client: DynamoDBClient, *, env_name: str, changed_at: str) -> SeedResult:
    try:
        require_deployable_environment(env_name)
    except EnvironmentRefusedError as error:
        raise SeedRefusedError(str(error)) from None
    problems = region_problems(client.meta.region_name, {})
    if problems:
        raise SeedRefusedError("; ".join(problems))

    written, preserved = [], []
    for item in config_items(changed_at):
        key = item["key"]["S"]
        try:
            client.put_item(
                TableName=f"aera-{env_name}-config",
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
        except (ClientError, BotoCoreError) as error:
            code = error_code(error)
            if code != "ConditionalCheckFailedException":
                raise SeedRefusedError(f"PutItem failed with {code} at {key}") from None
            preserved.append(key)
        else:
            written.append(key)
    return SeedResult(written=written, preserved=preserved)


def open_client(profile: str, region: str) -> DynamoDBClient:
    return boto3.Session(profile_name=profile, region_name=region).client("dynamodb")


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[str, str], DynamoDBClient] = open_client,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--env", required=True)
    parser.add_argument("--profile", default=os.environ.get("AERA_AWS_PROFILE"))
    parser.add_argument("--region", default=os.environ.get("AERA_REGION", APPROVED_REGION))
    args = parser.parse_args(argv)
    if not args.profile:
        parser.error("--profile or AERA_AWS_PROFILE is required")
    try:
        result = seed(
            client_factory(args.profile, args.region),
            env_name=args.env,
            changed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    except SeedRefusedError as error:
        print(error, file=sys.stderr)
        return 1
    kept = len(result.preserved)
    print(
        f"{len(result.written)} defaults written, "
        f"{kept} existing value{'' if kept == 1 else 's'} preserved"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
