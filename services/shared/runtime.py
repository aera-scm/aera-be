"""Lambda wiring: AWS clients, SSM parameters, secrets and the SAP client (SRD 6.16, 6.23).

Names follow `/aera/{env}/...`. Nothing here is imported at module level by business code,
so every component stays testable with injected fakes; handlers call these once per
container and cache the result.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from functools import cache
from typing import Any

import boto3

from services.shared.events import publish
from services.shared.models import EventEnvelope, EventType
from services.shared.sap_client import ClientCredentialsAuth, Endpoint, SapClient, Target


def env() -> str:
    return os.environ["AERA_ENV"]


@cache
def client(service: str) -> Any:
    return boto3.client(service)  # type: ignore[call-overload]


@cache
def parameter(key: str) -> str:
    response = client("ssm").get_parameter(Name=f"/aera/{env()}/{key}")
    return str(response["Parameter"]["Value"])


@cache
def secret(name: str) -> dict[str, Any]:
    value = client("secretsmanager").get_secret_value(SecretId=f"/aera/{env()}/{name}")
    return dict(json.loads(value["SecretString"]))


def raw_bucket() -> str:
    return os.environ["AERA_RAW_BUCKET"]


@cache
def sap_client() -> SapClient:
    """Mirror (or tenant) over OAuth client credentials; writes only where SAP_WRITE_BASE is set."""

    def credentials() -> tuple[str, str]:
        value = secret("sap/mirror-oauth-client")
        return str(value["clientId"]), str(value["clientSecret"])

    auth = ClientCredentialsAuth(
        token_url=str(secret("sap/mirror-oauth-client")["tokenUrl"]), credentials=credentials
    )
    read = Endpoint(base_url=parameter("SAP_READ_BASE"), target=Target.MIRROR, auth=auth)
    write = None
    if os.environ.get("AERA_SAP_WRITES") == "1":
        write = Endpoint(base_url=parameter("SAP_WRITE_BASE"), target=Target.MIRROR, auth=auth)
    return SapClient(read=read, write=write)


def emit(
    bus: Any,
    event_type: EventType,
    data: dict[str, Any],
    *,
    component: str,
    case_id: str | None = None,
    run_id: str | None = None,
    actor: str = "system",
    environment: str | None = None,
) -> EventEnvelope:
    envelope = EventEnvelope(
        type=event_type,
        time=datetime.now(UTC),
        env=environment or env(),
        case_id=case_id,
        run_id=run_id,
        actor=actor,
        data=data,
    )
    publish(bus, envelope, component=component)
    return envelope
