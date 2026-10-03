"""Seed the Config table idempotently (DR-10, DR-11, DR-12, NFR-MNT-02).

Writes the SRD 6.23 defaults, the reference-scenario rate card and the approver
limits. Each item is written only if its key is absent, so re-running never
overwrites a value an administrator changed. Only ``dev`` in the approved
region is accepted.

    uv run --locked python scripts/seed_config.py --env dev [--profile <name>]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
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

from infra.config_defaults import APPROVER_LIMITS, CONFIG_DEFAULTS, RATE_CARD  # noqa: E402
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


def _attribute(value: object) -> dict[str, str]:
    if isinstance(value, Decimal):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    raise TypeError(f"unsupported config value {value!r}")


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


def _record(prefix: str, key: str, fields: Mapping[str, object], **audit: str) -> dict[str, Any]:
    item: dict[str, Any] = {"PK": {"S": f"{prefix}#{key}"}}
    item.update({name: _attribute(value) for name, value in fields.items()})
    item.update({name: {"S": value} for name, value in audit.items()})
    return item


def rate_card_items(changed_at: str) -> list[dict[str, Any]]:
    return [
        _record("RATE", entry["entryId"], entry, changedBy=SEED_ACTOR, changedAt=changed_at)
        for entry in RATE_CARD
    ]


def approver_items(changed_at: str) -> list[dict[str, Any]]:
    return [
        _record("APPR", entry["userId"], entry, grantedBy=SEED_ACTOR, grantedAt=changed_at)
        for entry in APPROVER_LIMITS
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
    items = config_items(changed_at) + rate_card_items(changed_at) + approver_items(changed_at)
    for item in items:
        key = item["PK"]["S"]
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


REFERENCE_CASE_SEQUENCE = 913  # the next case opened is EXC-<year>-0914 (SRD 6.6.3)


def seed_case_counter(client: DynamoDBClient, *, env_name: str, year: int) -> bool:
    """Start the case counter so the reference case gets its SRD id; never lowers it."""
    try:
        require_deployable_environment(env_name)
    except EnvironmentRefusedError as error:
        raise SeedRefusedError(str(error)) from None
    try:
        client.put_item(
            TableName=f"aera-{env_name}-cases",
            Item={
                "PK": {"S": f"COUNTER#CASE#{year}"},
                "SK": {"S": "COUNTER"},
                "seq": {"N": str(REFERENCE_CASE_SEQUENCE)},
            },
            ConditionExpression="attribute_not_exists(PK)",
        )
    except (ClientError, BotoCoreError) as error:
        code = error_code(error)
        if code != "ConditionalCheckFailedException":
            raise SeedRefusedError(f"PutItem failed with {code} at the case counter") from None
        return False
    return True


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
    parser.add_argument(
        "--case-counter-year",
        type=int,
        help="also start that year's case counter so the reference case is EXC-<year>-0914",
    )
    args = parser.parse_args(argv)
    if not args.profile:
        parser.error("--profile or AERA_AWS_PROFILE is required")
    client = client_factory(args.profile, args.region)
    try:
        result = seed(
            client,
            env_name=args.env,
            changed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        if args.case_counter_year:
            started = seed_case_counter(client, env_name=args.env, year=args.case_counter_year)
            print("case counter started" if started else "case counter already running")
    except SeedRefusedError as error:
        print(error, file=sys.stderr)
        return 1
    kept = len(result.preserved)
    print(
        f"{len(result.written)} items written, "
        f"{kept} existing value{'' if kept == 1 else 's'} preserved"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
