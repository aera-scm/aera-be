"""Replay the synthetic signal set into a deployed environment (WP-3, M1 exit checklist).

Emails are written to the raw bucket under `ses/`, exactly where the SES receipt rule puts
real mail; WhatsApp payloads and carrier events are signed with the keys stored in Secrets
Manager and posted to the deployed webhooks. WhatsApp images go to `replay-media/`, which the
webhooks read when deployed with AERA_WHATSAPP_MEDIA=replay. Secret values are used in memory
and never printed.

    uv run --locked python scripts/replay_signals.py --env dev --profile <p> --t0 <SCENARIO_T0>
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from generate_signals import CARRIER_ID, Item, build  # noqa: E402

from infra.environments import (  # noqa: E402
    APPROVED_REGION,
    EnvironmentRefusedError,
    require_deployable_environment,
)


@dataclass
class Target:
    api_url: str
    raw_bucket: str
    app_secret: str
    carrier_key: str


def _sign(key: str, message: bytes) -> str:
    return "sha256=" + hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def replay(
    items: Sequence[Item],
    target: Target,
    *,
    s3: Any,
    http: httpx.Client,
    clock: Callable[[], float] = time.time,
    run_id: str = "",
) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    for item in items:
        main = next(content for name, content in item.files.items() if "/" not in name)
        for name, content in item.files.items():
            if name.startswith("media/"):
                s3.put_object(Bucket=target.raw_bucket, Key=f"replay-{name}", Body=content)
        if item.channel == "EMAIL":
            key = f"ses/replay-{run_id}-{item.id}"
            s3.put_object(Bucket=target.raw_bucket, Key=key, Body=main)
            results.append((item.id, f"stored {key}"))
            continue
        if item.channel == "WHATSAPP":
            response = http.post(
                f"{target.api_url.rstrip('/')}/webhooks/whatsapp",
                content=main,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": _sign(target.app_secret, main),
                },
            )
        else:
            stamp = str(int(clock()))
            response = http.post(
                f"{target.api_url.rstrip('/')}/webhooks/carrier",
                content=main,
                headers={
                    "Content-Type": "application/json",
                    "X-Aera-Timestamp": stamp,
                    "X-Aera-Signature": _sign(target.carrier_key, stamp.encode() + b"." + main),
                },
            )
        results.append((item.id, f"HTTP {response.status_code}"))
    return results


def resolve(session: Any, env_name: str) -> Target:
    ssm = session.client("ssm")
    secrets = session.client("secretsmanager")

    def secret(name: str) -> dict[str, Any]:
        value = secrets.get_secret_value(SecretId=f"/aera/{env_name}/{name}")["SecretString"]
        return dict(json.loads(value))

    account = session.client("sts").get_caller_identity()["Account"]
    region = session.region_name
    return Target(
        api_url=ssm.get_parameter(Name=f"/aera/{env_name}/API_URL")["Parameter"]["Value"],
        raw_bucket=f"aera-{env_name}-raw-{account}-{region}",
        app_secret=str(secret("channels/whatsapp")["appSecret"]),
        carrier_key=str(secret("channels/carrier-webhook")[CARRIER_ID]),
    )


def main(argv: Sequence[str] | None = None) -> int:
    import boto3

    parser = argparse.ArgumentParser(description="Replay synthetic signals into an environment.")
    parser.add_argument("--env", required=True)
    parser.add_argument("--profile", default=os.environ.get("AERA_AWS_PROFILE"))
    parser.add_argument("--region", default=APPROVED_REGION)
    parser.add_argument("--t0", required=True, help="the Mirror's SCENARIO_T0 (ISO 8601)")
    parser.add_argument("--only", nargs="*", help="item ids, e.g. email-01 hostile-01")
    args = parser.parse_args(argv)
    try:
        require_deployable_environment(args.env)
    except EnvironmentRefusedError as error:
        print(error, file=sys.stderr)
        return 1
    t0 = datetime.fromisoformat(args.t0.replace("Z", "+00:00")).astimezone(UTC)
    items = [i for i in build(t0) if not args.only or i.id in args.only]
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    target = resolve(session, args.env)
    with httpx.Client(timeout=15.0) as http:
        results = replay(
            items,
            target,
            s3=session.client("s3"),
            http=http,
            run_id=datetime.now(UTC).strftime("%Y%m%d%H%M%S"),
        )
    for item_id, outcome in results:
        print(f"{item_id}: {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
