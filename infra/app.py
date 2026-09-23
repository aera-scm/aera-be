"""Main CDK app (SRD 6.16). Deployed by ``scripts/deploy_dev.py deploy``.

Run from the repository root: ``python -m infra.app``. Inputs come from the
environment: ``AERA_ENV``, ``AERA_OWNER_TAG`` (required), optional approved
``MODEL_SUPERVISOR_ID`` / ``MODEL_SMALL_ID`` (OT-03) and optional names of
approved existing secrets ``AERA_SAP_SANDBOX_SECRET_NAME`` /
``AERA_SAP_MIRROR_SECRET_NAME``. The budget lives in ``infra/budget_app.py``.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from aws_cdk import App, Environment, Tags

from infra.environments import APPROVED_REGION, require_deployable_environment
from infra.stacks.data import DataStack


@dataclass(frozen=True)
class DataSettings:
    env_name: str
    owner: str
    model_supervisor_id: str | None = None
    model_small_id: str | None = None
    sandbox_secret_name: str | None = None
    mirror_secret_name: str | None = None


def _optional(environ: Mapping[str, str], name: str) -> str | None:
    return environ.get(name, "").strip() or None


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
    )


def build_app(settings: DataSettings) -> App:
    env_name = require_deployable_environment(settings.env_name)
    app = App(analytics_reporting=False)
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
        description="AERA data layer: tables, buckets, key, bus, parameters (SRD 6.20)",
    )
    for key, value in (
        ("project", "aera"),
        ("env", env_name),
        ("component", "data"),
        ("owner", settings.owner),
    ):
        Tags.of(data).add(key, value)
    return app


def main() -> None:
    build_app(data_settings_from_environment(os.environ)).synth()


if __name__ == "__main__":
    main()
