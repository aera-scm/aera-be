"""Upload a console build to the web bucket and refresh CloudFront (SRD 6.19).

Build the console in aera-fe in live mode first (`pnpm build` with the VITE_* values of the
environment), then:

    uv run --locked python scripts/deploy_console.py --env dev --profile <p> --dist ../aera-fe/dist

The bucket and distribution come from the outputs of the `aera-{env}-web` stack. Only dev
is deployed from here. Prints no secret.
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infra.environments import require_deployable_environment  # noqa: E402

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}


def plan_upload(env: str, dist: Path) -> list[tuple[str, Path, str, str]]:
    """(key, file, content type, cache control) for every file of the build."""
    require_deployable_environment(env)
    if not (dist / "index.html").is_file():
        raise ValueError(f"{dist} has no index.html; build the console first")
    plan = []
    for path in sorted(dist.rglob("*")):
        relative = path.relative_to(dist)
        if (
            path.is_symlink()
            or any(part.lower() == "secrets" for part in relative.parts)
            or path.name.lower().startswith(".env")
            or path.suffix.lower() in {".pem", ".key"}
        ):
            raise ValueError("console build contains a sensitive file or symbolic link")
        if not path.is_file():
            continue
        key = path.relative_to(dist).as_posix()
        kind = TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
        if key == "index.html":
            cache = "no-cache"  # always fetch the shell that names the current assets
        elif key.startswith("assets/"):
            cache = "public, max-age=31536000, immutable"  # file names carry a content hash
        else:
            cache = "public, max-age=3600"
        plan.append((key, path, kind or "application/octet-stream", cache))
    return plan


def outputs(cloudformation: object, stack: str) -> dict[str, str]:
    described = cloudformation.describe_stacks(StackName=stack)  # type: ignore[attr-defined]
    return {o["OutputKey"]: o["OutputValue"] for o in described["Stacks"][0].get("Outputs", [])}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--dist", required=True, type=Path)
    args = parser.parse_args(argv)
    plan = plan_upload(args.env, args.dist)

    session = boto3.Session(profile_name=args.profile, region_name="us-east-1")
    found = outputs(session.client("cloudformation"), f"aera-{args.env}-web")
    bucket, distribution = found["ConsoleBucket"], found["ConsoleDistributionId"]
    s3 = session.client("s3")
    # Assets first, index last, so a visitor never gets a shell naming missing files.
    for key, path, kind, cache in sorted(plan, key=lambda item: item[0] == "index.html"):
        s3.upload_file(
            str(path), bucket, key, ExtraArgs={"ContentType": kind, "CacheControl": cache}
        )
    session.client("cloudfront").create_invalidation(
        DistributionId=distribution,
        InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": f"console-{int(time.time())}",
        },
    )
    print(f"uploaded {len(plan)} files; console at {found['ConsoleUrl']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
