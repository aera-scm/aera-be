"""Main CDK app (SRD 6.16). Deployed by ``scripts/deploy_dev.py deploy``.

Run from the repository root: ``python -m infra.app``. Inputs come from the
environment: ``AERA_ENV``, ``AERA_OWNER_TAG`` (required), optional approved
``MODEL_SUPERVISOR_ID`` / ``MODEL_SMALL_ID`` and optional names of
approved existing secrets ``AERA_SAP_SANDBOX_SECRET_NAME`` /
``AERA_SAP_MIRROR_SECRET_NAME``. The budget lives in ``infra/budget_app.py``.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aws_cdk import App, DefaultStackSynthesizer, Environment, Stack, Tags

from infra.constructs.github_oidc import GitHubOidc
from infra.constructs.service_function import service_code
from infra.environments import APPROVED_REGION, require_deployable_environment
from infra.stacks.control import ControlStack
from infra.stacks.data import DataStack
from infra.stacks.edge import EdgeStack
from infra.stacks.gate import GateStack
from infra.stacks.identity import IdentityStack
from infra.stacks.interop import InteropStack
from infra.stacks.observability import ObservabilityStack
from infra.stacks.reasoning import ReasoningStack
from infra.stacks.web import WebStack


@dataclass(frozen=True)
class DataSettings:
    env_name: str
    owner: str
    model_supervisor_id: str | None = None
    model_small_id: str | None = None
    sandbox_secret_name: str | None = None
    mirror_secret_name: str | None = None
    github_repository: str | None = None
    cdk_qualifier: str | None = None
    github_provider_mode: str | None = None
    budget_name: str | None = None
    lambda_bundle: str | None = None
    console_origins: tuple[str, ...] = ()
    inbound_recipients: tuple[str, ...] = ()
    whatsapp_media: str = "graph"


def _optional(environ: Mapping[str, str], name: str) -> str | None:
    return environ.get(name, "").strip() or None


def _list(environ: Mapping[str, str], name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in environ.get(name, "").split(",") if part.strip())


def data_settings_from_environment(environ: Mapping[str, str]) -> DataSettings:
    owner = _optional(environ, "AERA_OWNER_TAG")
    if owner is None:
        raise ValueError("AERA_OWNER_TAG is required: the owner tag on every resource (SRD 6.16)")
    return DataSettings(
        env_name=environ.get("AERA_ENV", ""),
        owner=owner,
        model_supervisor_id=_optional(environ, "MODEL_SUPERVISOR_ID"),
        model_small_id=_optional(environ, "MODEL_SMALL_ID"),
        sandbox_secret_name=_optional(environ, "AERA_SAP_SANDBOX_SECRET_NAME"),
        mirror_secret_name=_optional(environ, "AERA_SAP_MIRROR_SECRET_NAME"),
        github_repository=_optional(environ, "AERA_GITHUB_REPOSITORY"),
        cdk_qualifier=_optional(environ, "AERA_CDK_QUALIFIER"),
        github_provider_mode=_optional(environ, "AERA_GITHUB_PROVIDER_MODE"),
        budget_name=_optional(environ, "AERA_BUDGET_NAME"),
        lambda_bundle=_optional(environ, "AERA_LAMBDA_BUNDLE"),
        console_origins=_list(environ, "AERA_CONSOLE_ORIGINS"),
        inbound_recipients=_list(environ, "AERA_INBOUND_RECIPIENTS"),
        whatsapp_media=_optional(environ, "AERA_WHATSAPP_MEDIA") or "graph",
    )


def build_app(settings: DataSettings) -> App:
    env_name = require_deployable_environment(settings.env_name)
    app = App(analytics_reporting=False)
    if settings.github_repository and not all(
        (settings.cdk_qualifier, settings.github_provider_mode, settings.budget_name)
    ):
        raise ValueError("OIDC requires approved qualifier, provider mode and budget name")
    if settings.github_provider_mode and not settings.github_repository:
        raise ValueError("OIDC requires an approved repository")
    model_ids = {
        key: value
        for key, value in (
            ("MODEL_SUPERVISOR_ID", settings.model_supervisor_id),
            ("MODEL_SMALL_ID", settings.model_small_id),
        )
        if value
    }
    existing = {
        component: name
        for component, name in (
            ("sandbox-api-key", settings.sandbox_secret_name),
            ("mirror-oauth-client", settings.mirror_secret_name),
        )
        if name
    }
    data = DataStack(
        app,
        f"aera-{env_name}-data",
        env_name=env_name,
        model_ids=model_ids,
        existing_secrets=existing,
        env=Environment(region=APPROVED_REGION),
        synthesizer=DefaultStackSynthesizer(qualifier=settings.cdk_qualifier),
        description="AERA data layer: tables, buckets, key, bus, parameters (SRD 6.20)",
    )
    common: dict[str, Any] = {
        "env_name": env_name,
        "env": Environment(region=APPROVED_REGION),
        "synthesizer": DefaultStackSynthesizer(qualifier=settings.cdk_qualifier),
    }

    identity = IdentityStack(app, f"aera-{env_name}-identity", **common)
    stacks: dict[str, Stack] = {
        "data": data,
        "identity": identity,
        "edge": EdgeStack(
            app,
            f"aera-{env_name}-edge",
            data=data,
            pool_arn=identity.pool_arn,
            code=service_code(settings.lambda_bundle),
            console_origins=list(settings.console_origins),
            inbound_recipients=list(settings.inbound_recipients),
            whatsapp_media=settings.whatsapp_media,
            **common,
        ),
        "gate": GateStack(
            app,
            f"aera-{env_name}-gate",
            data=data,
            code=service_code(settings.lambda_bundle),
            **common,
        ),
    }
    gate = stacks["gate"]
    assert isinstance(gate, GateStack)
    stacks["reasoning"] = ReasoningStack(
        app,
        f"aera-{env_name}-reasoning",
        data=data,
        guardrail_arn=gate.guardrail_arn,
        code=service_code(settings.lambda_bundle),
        **common,
    )
    for component, stack_type in (
        ("control", ControlStack),
        ("interop", InteropStack),
        ("web", WebStack),
        ("observability", ObservabilityStack),
    ):
        stacks[component] = stack_type(app, f"aera-{env_name}-{component}", **common)
    if settings.github_repository:
        GitHubOidc(
            stacks["identity"],
            "GitHub",
            env_name=env_name,
            repository=settings.github_repository,
            qualifier=settings.cdk_qualifier or "",
            provider_mode=settings.github_provider_mode or "",
            budget_name=settings.budget_name or "",
        )
    dependencies = {
        "identity": ("data",),
        "edge": ("data", "identity"),
        "gate": ("data",),
        "reasoning": ("data", "gate"),
        "control": ("data", "reasoning"),
        "interop": ("edge", "identity"),
        "web": ("edge", "identity"),
        "observability": tuple(name for name in stacks if name != "observability"),
    }
    for component, prerequisites in dependencies.items():
        for prerequisite in prerequisites:
            stacks[component].add_stack_dependency(stacks[prerequisite])
    for component, stack in stacks.items():
        for key, value in (
            ("project", "aera"),
            ("env", env_name),
            ("component", component),
            ("owner", settings.owner),
        ):
            Tags.of(stack).add(key, value)
    return app


def main() -> None:
    build_app(data_settings_from_environment(os.environ)).synth()


if __name__ == "__main__":
    main()
