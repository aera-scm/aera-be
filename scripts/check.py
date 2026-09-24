"""Run local and CI checks consistently (NFR-SEC-03, NFR-SEC-06)."""

import subprocess
import sys
from pathlib import Path

from security import repository_files

ROOT = Path(__file__).resolve().parents[1]


def run(*command: str) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def check(name: str) -> None:
    files = repository_files()
    if name == "lint":
        run("ruff", "check", ".")
        run("ruff", "format", "--check", ".")
        yaml = [path for path in files if path.endswith((".yml", ".yaml"))]
        if yaml:
            run("yamllint", "--strict", *yaml)
    elif name == "typecheck":
        run("mypy", *[path for path in files if path.endswith(".py")])
    elif name == "test":
        if any(Path(path).match("test_*.py") or path.endswith("_test.py") for path in files):
            # NFR-MNT-01: at least 90% line coverage in the rules package.
            run(
                "pytest",
                "--cov=services.rules",
                "--cov-report=term-missing:skip-covered",
                "--cov-fail-under=90",
            )
        else:
            print("No unit or contract tests exist yet; no application test coverage is claimed.")
        if (ROOT / "sap-mirror" / "package.json").exists():
            run("pnpm", "--dir", "sap-mirror", "test")
    elif name == "secrets":
        run(sys.executable, "scripts/security.py")
    elif name == "audit":
        run("pip-audit", "--local")
        run("pnpm", "audit", "--audit-level", "low")
        if (ROOT / "sap-mirror" / "package.json").exists():
            run("pnpm", "--dir", "sap-mirror", "audit", "--audit-level", "low")
    else:
        raise ValueError(f"Unknown check: {name}")


if __name__ == "__main__":
    try:
        for selected in sys.argv[1:] or ["lint", "typecheck", "test", "secrets", "audit"]:
            check(selected)
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
