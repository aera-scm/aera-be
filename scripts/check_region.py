"""Region configuration and service availability check (OI-04, A-02, NFR-CMP-02).

Offline (default) it validates configuration only: the deployment region is the
approved region of ADR-001, and the SDK region variables do not point elsewhere.
That is not account evidence. With ``--live`` it verifies the budget first
(NFR-COST-01), then makes one read-only list call per required service in the
region to prove the account can reach it.

    uv run --locked python scripts/check_region.py [--live --profile <name> --budget-name <name>]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from check_budget import (
    BudgetCheckError,
    add_budget_arguments,
    require_budget_arguments,
    verify_with_clients,
)

if TYPE_CHECKING:
    from botocore.client import BaseClient
    from mypy_boto3_bedrock import BedrockClient
    from mypy_boto3_bedrock_agentcore_control import BedrockAgentCoreControlClient
    from mypy_boto3_budgets import BudgetsClient
    from mypy_boto3_comprehend import ComprehendClient
    from mypy_boto3_sts import STSClient
    from mypy_boto3_textract import TextractClient

# ADR-001 / OT-02: one region for everything.
APPROVED_REGION = "us-east-1"
SDK_REGION_VARIABLES = ("AWS_REGION", "AWS_DEFAULT_REGION")

_REGION = re.compile(r"[a-z]{2}(-gov)?-[a-z]+-\d")


def region_problems(region: str, environ: Mapping[str, str]) -> list[str]:
    if not _REGION.fullmatch(region):
        return [f"{region!r} is not a valid AWS region name"]
    problems = []
    if region != APPROVED_REGION:
        problems.append(
            f"region '{region}' is not the approved region '{APPROVED_REGION}' "
            "(ADR-001, NFR-CMP-02); changing it needs a new owner decision"
        )
    for variable in SDK_REGION_VARIABLES:
        value = environ.get(variable)
        if value and value != region:
            problems.append(f"{variable} is '{value}' but the deployment region is '{region}'")
    return problems


def client_region_problems(clients: Iterable[BaseClient], region: str) -> list[str]:
    return [
        f"{client.meta.service_model.service_name} client is in "
        f"'{client.meta.region_name}', not '{region}'"
        for client in clients
        if client.meta.region_name != region
    ]


def error_code(error: ClientError | BotoCoreError) -> str:
    # Only the code is reported; AWS messages can contain the account id.
    if isinstance(error, ClientError):
        return str(error.response.get("Error", {}).get("Code", "Unknown"))
    return type(error).__name__


@dataclass(frozen=True)
class RegionClients:
    sts: STSClient
    budgets: BudgetsClient
    bedrock: BedrockClient
    agentcore: BedrockAgentCoreControlClient
    textract: TextractClient
    comprehend: ComprehendClient

    def regional(self) -> tuple[BaseClient, ...]:
        return (self.bedrock, self.agentcore, self.textract, self.comprehend)


def open_region_clients(profile: str, region: str) -> RegionClients:
    session = boto3.Session(profile_name=profile, region_name=region)
    return RegionClients(
        sts=session.client("sts"),
        budgets=session.client("budgets"),
        bedrock=session.client("bedrock"),
        agentcore=session.client("bedrock-agentcore-control"),
        textract=session.client("textract"),
        comprehend=session.client("comprehend"),
    )


def service_probes(clients: RegionClients) -> list[tuple[str, Callable[[], Any]]]:
    """One read-only, free list call per A-02 service; nothing is created."""
    return [
        ("Bedrock Guardrails", lambda: clients.bedrock.list_guardrails(maxResults=1)),
        (
            "Automated Reasoning",
            lambda: clients.bedrock.list_automated_reasoning_policies(maxResults=1),
        ),
        ("AgentCore", lambda: clients.agentcore.list_agent_runtimes(maxResults=1)),
        ("Textract", lambda: clients.textract.list_adapters(MaxResults=1)),
        ("Comprehend", lambda: clients.comprehend.list_document_classifiers(MaxResults=1)),
    ]


def probe_services(clients: RegionClients, region: str) -> tuple[list[str], bool]:
    lines, ok = [], True
    for name, probe in service_probes(clients):
        try:
            probe()
        except (ClientError, BotoCoreError) as error:
            ok = False
            lines.append(f"Account check ({region}): {name}: failed with {error_code(error)}")
        else:
            lines.append(f"Account check ({region}): {name}: reachable")
    return lines, ok


def main(
    argv: Sequence[str] | None = None,
    *,
    clients_factory: Callable[[str, str], RegionClients] = open_region_clients,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    add_budget_arguments(parser)
    parser.add_argument("--live", action="store_true", help="run account checks after the budget")
    args = parser.parse_args(argv)

    problems = region_problems(args.region, os.environ)
    if problems:
        print("; ".join(problems), file=sys.stderr)
        return 1
    print(
        f"Offline configuration: region {args.region} is the approved region "
        "(not account evidence)."
    )
    if not args.live:
        return 0

    require_budget_arguments(parser, args)
    clients = clients_factory(args.profile, args.region)
    mismatched = client_region_problems(clients.regional(), args.region)
    if mismatched:
        print("; ".join(mismatched), file=sys.stderr)
        return 1
    try:
        verify_with_clients(
            clients.sts,
            clients.budgets,
            budget_name=args.budget_name,
            expected_limit_usd=args.expected_limit_usd,
        )
    except BudgetCheckError as error:
        print(error, file=sys.stderr)
        return 1
    lines, ok = probe_services(clients, args.region)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
