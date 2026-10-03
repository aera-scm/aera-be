"""Build the Lambda bundle: the `services` package plus its runtime dependencies for
Python 3.12 on ARM64 (SRD 6.16, 7.1).

Dependencies are the project's pinned runtime requirements minus the CDK toolchain, installed
from binary wheels for `aarch64-manylinux2014` with the pinned uv. Tests are left out.

    uv run --locked python scripts/build_lambda.py            # writes build/lambda
    AERA_LAMBDA_BUNDLE=build/lambda uv run --locked python scripts/deploy_dev.py deploy ...
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "build" / "lambda"
# Infrastructure tooling, not needed at runtime.
BUILD_ONLY = {"aws-cdk-lib", "constructs"}
# Present in every bundle; a directory without them is not one.
MARKERS = ("services/shared", "pydantic", "aws_lambda_powertools")


def runtime_requirements(pyproject: Path = ROOT / "pyproject.toml") -> list[str]:
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    # The agent runtime deploys the same bundle (SRD 6.18), so its SDKs come along.
    wanted = [*config["project"]["dependencies"], *config["dependency-groups"]["agent"]]
    requirements = []
    for requirement in wanted:
        name = requirement.split("==")[0].split("[")[0].strip().lower()
        if "==" not in requirement:
            raise ValueError(f"{requirement} is not pinned")
        if name not in BUILD_ONLY:
            requirements.append(requirement)
    return requirements


def bundle_problems(path: Path) -> list[str]:
    missing = [marker for marker in MARKERS if not (path / marker).exists()]
    if missing:
        return [f"{path} is not a Lambda bundle (missing {', '.join(missing)}); run build_lambda"]
    return []


def build(out: Path) -> None:
    if out.exists():
        raise SystemExit(f"{out} exists; choose a new --out directory (nothing is deleted)")
    out.mkdir(parents=True)
    requirements = out.parent / f"{out.name}-requirements.txt"
    requirements.write_text("\n".join(runtime_requirements()) + "\n", encoding="utf-8")
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(out),
            "--python-version",
            "3.12",
            "--python-platform",
            "aarch64-manylinux2014",
            "--only-binary",
            ":all:",
            "--requirement",
            str(requirements),
        ],
        check=True,
    )
    shutil.copytree(
        ROOT / "services",
        out / "services",
        ignore=shutil.ignore_patterns("tests", "__pycache__", "conftest.py", "*.pyc"),
    )
    problems = bundle_problems(out)
    if problems:
        raise SystemExit("; ".join(problems))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the AERA Lambda bundle.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    build(args.out)
    print(f"Lambda bundle written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
