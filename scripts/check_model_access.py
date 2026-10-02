"""Model id configuration, account access and smoke invocation (A-01, OI-04, NFR-CMP-02).

Offline (default) it validates ``MODEL_SUPERVISOR_ID`` and ``MODEL_SMALL_ID``:
bare, direct regional Bedrock model ids of the right family. Geographic and
global inference profiles and ARNs are rejected, with one exception: the Claude
supervisor may use the US geographic profile (ADR-0023). That is not account
evidence.

``--live`` verifies the budget first (NFR-COST-01), then checks each model in the
approved region: offered on demand, active, and authorised with provider
agreement, entitlement and region availability. A US profile must be active,
system-defined and route only to the approved US regions. ``--invoke`` additionally sends
one tiny synthetic Converse request per model, bounded to a few output tokens.
Account and invocation results are reported on separate, labelled lines.

    uv run --locked python scripts/check_model_access.py [--live --profile <name>
        --budget-name <name> [--invoke]]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Sequence
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
from check_region import client_region_problems, error_code, region_problems

if TYPE_CHECKING:
    from mypy_boto3_bedrock import BedrockClient
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient
    from mypy_boto3_budgets import BudgetsClient
    from mypy_boto3_sts import STSClient

SUPERVISOR_VARIABLE = "MODEL_SUPERVISOR_ID"
SMALL_VARIABLE = "MODEL_SMALL_ID"

# Prefixes of geographic and global cross-region inference profiles (A-01).
GEOGRAPHIC_PREFIXES = frozenset({"us", "us-gov", "eu", "apac", "jp", "au", "ca", "global"})
# ADR-0023: the one permitted profile, for the Claude supervisor only, and the regions it
# may route to. Mirrors infra.stacks.reasoning; a test keeps the two equal.
US_PROFILE_PREFIX = "us"
US_PROFILE_REGIONS = ("us-east-1", "us-east-2", "us-west-2")
# SRD 6.23 / 6.24: Claude supervisor; Nova or smaller Claude for classification.
MODEL_FAMILIES: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    SUPERVISOR_VARIABLE: ("an Anthropic Claude model", (("anthropic", "claude-"),)),
    SMALL_VARIABLE: (
        "an Amazon Nova or Anthropic Claude model",
        (("amazon", "nova-"), ("anthropic", "claude-")),
    ),
}

SMOKE_PROMPT = "Reply with the single word OK."
SMOKE_MAX_TOKENS = 8

_MODEL_ID = re.compile(
    r"(?P<first>[a-z0-9-]{1,63})\.(?P<second>[a-z0-9-]{1,63})"
    r"(?:\.(?P<third>[a-z0-9-]{1,63}))?(?::[a-z0-9-]{1,63}){0,2}"
)


def _model_id_problem(variable: str, model_id: str) -> str | None:
    value = model_id.strip()
    if not value:
        return f"{variable} is required: an approved direct regional model id"
    if value.startswith("arn:"):
        return f"{variable} must be a bare model id, not an ARN (A-01)"
    match = _MODEL_ID.fullmatch(value)
    if match is None:
        return f"{variable} {value!r} is not a Bedrock model id"
    provider, model = match["first"], match["second"]
    if variable == SUPERVISOR_VARIABLE and profile_base_model(value) is not None:
        provider, model = match["second"], match["third"]
    elif match["third"] is not None or match["first"] in GEOGRAPHIC_PREFIXES:
        return (
            f"{variable} {value!r} is a geographic or global inference profile; only direct "
            "regional model ids are allowed, or the US profile for the supervisor "
            "(A-01, NFR-CMP-02, ADR-0023)"
        )
    family, allowed = MODEL_FAMILIES[variable]
    if not any(provider == p and model.startswith(m) for p, m in allowed):
        return f"{variable} {value!r} is not {family} (SRD 6.23)"
    return None


def profile_base_model(model_id: str) -> str | None:
    """Return the model behind a US inference profile id, or None for a direct id."""
    match = _MODEL_ID.fullmatch(model_id)
    if match is None or match["third"] is None or match["first"] != US_PROFILE_PREFIX:
        return None
    return model_id.split(".", 1)[1]


def model_id_problems(supervisor: str, small: str) -> list[str]:
    problems = (
        _model_id_problem(SUPERVISOR_VARIABLE, supervisor),
        _model_id_problem(SMALL_VARIABLE, small),
    )
    return [problem for problem in problems if problem is not None]


@dataclass(frozen=True)
class ModelClients:
    sts: STSClient
    budgets: BudgetsClient
    bedrock: BedrockClient
    runtime: BedrockRuntimeClient


def open_model_clients(profile: str, region: str) -> ModelClients:
    session = boto3.Session(profile_name=profile, region_name=region)
    return ModelClients(
        sts=session.client("sts"),
        budgets=session.client("budgets"),
        bedrock=session.client("bedrock"),
        runtime=session.client("bedrock-runtime"),
    )


class _ModelUnavailableError(Exception):
    """A model or profile lookup failed; the message is the problem to report."""


def _model_details(bedrock: BedrockClient, model_id: str, region: str) -> Any:
    try:
        return bedrock.get_foundation_model(modelIdentifier=model_id)["modelDetails"]
    except (ClientError, BotoCoreError) as error:
        code = error_code(error)
        if code == "ResourceNotFoundException":
            raise _ModelUnavailableError(f"not offered in {region}") from None
        raise _ModelUnavailableError(f"GetFoundationModel failed with {code}") from None


def _profile_problems(bedrock: BedrockClient, profile_id: str, region: str) -> list[str]:
    """ADR-0023: the profile must be AWS-defined, active and stay in the approved US regions."""
    try:
        profile = bedrock.get_inference_profile(inferenceProfileIdentifier=profile_id)
    except (ClientError, BotoCoreError) as error:
        code = error_code(error)
        if code == "ResourceNotFoundException":
            raise _ModelUnavailableError(f"inference profile not offered in {region}") from None
        raise _ModelUnavailableError(f"GetInferenceProfile failed with {code}") from None
    problems = []
    if profile["status"] != "ACTIVE":
        problems.append(f"inference profile status is {profile['status']!r}, expected 'ACTIVE'")
    if profile["type"] != "SYSTEM_DEFINED":
        problems.append(f"inference profile type is {profile['type']!r}, expected 'SYSTEM_DEFINED'")
    routed = sorted({model["modelArn"].split(":")[3] for model in profile["models"]})
    problems.extend(
        f"inference profile routes to '{target}', outside {', '.join(US_PROFILE_REGIONS)}"
        for target in routed
        if target not in US_PROFILE_REGIONS
    )
    return problems


def check_model(bedrock: BedrockClient, model_id: str, region: str) -> tuple[str, list[str]]:
    """Return the account-check line and any problems for one model."""
    base = profile_base_model(model_id)
    try:
        problems = [] if base is None else _profile_problems(bedrock, model_id, region)
        details = _model_details(bedrock, base or model_id, region)
    except _ModelUnavailableError as error:
        return "", [f"{model_id}: {error}"]

    if base is not None:
        if "INFERENCE_PROFILE" not in details.get("inferenceTypesSupported", []):
            problems.append("not invocable through an inference profile")
    elif "ON_DEMAND" not in details.get("inferenceTypesSupported", []):
        problems.append("not invocable on demand by direct regional inference")
    status = details.get("modelLifecycle", {}).get("status")
    if status != "ACTIVE":
        problems.append(f"lifecycle is {status!r}, expected 'ACTIVE'")
    arn_region = details["modelArn"].split(":")[3]
    if arn_region != region:
        problems.append(f"resolved in '{arn_region}', not '{region}'")

    try:
        access = bedrock.get_foundation_model_availability(modelId=base or model_id)
    except (ClientError, BotoCoreError) as error:
        problems.append(f"GetFoundationModelAvailability failed with {error_code(error)}")
    else:
        expected = (
            ("authorization", access["authorizationStatus"], "AUTHORIZED"),
            ("provider agreement", access["agreementAvailability"]["status"], "AVAILABLE"),
            ("entitlement", access["entitlementAvailability"], "AVAILABLE"),
            ("region availability", access["regionAvailability"], "AVAILABLE"),
        )
        problems.extend(
            f"{label} is {actual!r}" for label, actual, wanted in expected if actual != wanted
        )

    kind = (
        "on demand" if base is None else f"US inference profile ({', '.join(US_PROFILE_REGIONS)})"
    )
    line = (
        f"Account check ({region}): {model_id}: {kind}, ACTIVE, authorized; provider "
        "agreement, entitlement and region available"
    )
    return ("", [f"{model_id}: {problem}" for problem in problems]) if problems else (line, [])


def invoke_model(runtime: BedrockRuntimeClient, model_id: str, region: str) -> tuple[str, str]:
    """Send one bounded synthetic request; return (result line, problem)."""
    try:
        response = runtime.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": SMOKE_PROMPT}]}],
            inferenceConfig={"maxTokens": SMOKE_MAX_TOKENS, "temperature": 0.0},
        )
    except (ClientError, BotoCoreError) as error:
        return "", f"{model_id}: Converse failed with {error_code(error)}"
    tokens = response["usage"]["outputTokens"]
    plural = "" if tokens == 1 else "s"
    return (
        f"Invocation ({region}): {model_id}: {response['stopReason']}, "
        f"{tokens} output token{plural}",
        "",
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    clients_factory: Callable[[str, str], ModelClients] = open_model_clients,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    add_budget_arguments(parser)
    parser.add_argument("--supervisor-model-id", default=os.environ.get(SUPERVISOR_VARIABLE, ""))
    parser.add_argument("--small-model-id", default=os.environ.get(SMALL_VARIABLE, ""))
    parser.add_argument("--live", action="store_true", help="check account access after the budget")
    parser.add_argument("--invoke", action="store_true", help="with --live: one tiny Converse call")
    args = parser.parse_args(argv)
    if args.invoke and not args.live:
        parser.error("--invoke requires --live")

    region = args.region
    models = (args.supervisor_model_id.strip(), args.small_model_id.strip())
    problems = region_problems(region, os.environ) + model_id_problems(*models)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(
        f"Offline configuration: region {region}; supervisor {models[0]}; small {models[1]}; "
        "direct regional model ids, or the US profile for the supervisor "
        "(not account evidence)."
    )
    if not args.live:
        return 0

    require_budget_arguments(parser, args)
    clients = clients_factory(args.profile, region)
    mismatched = client_region_problems((clients.bedrock, clients.runtime), region)
    if mismatched:
        print("\n".join(mismatched), file=sys.stderr)
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

    for model_id in models:
        line, model_problems = check_model(clients.bedrock, model_id, region)
        if line:
            print(line)
        problems.extend(model_problems)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    if not args.invoke:
        return 0

    for model_id in models:
        line, problem = invoke_model(clients.runtime, model_id, region)
        if line:
            print(line)
        if problem:
            problems.append(problem)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
