"""Deployment workflow event and permission boundaries (NFR-SEC-03/06)."""

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]  # Installed by the locked YAML lint tooling.


def workflow(name: str) -> dict[str, Any]:
    # BaseLoader preserves the GitHub 'on' key (YAML 1.1 treats it as a boolean).
    with (Path(".github/workflows") / name).open(encoding="utf-8") as source:
        return dict(yaml.load(source, Loader=yaml.BaseLoader))


def test_validation_cannot_obtain_deployment_credentials() -> None:
    ci = workflow("ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in ci["jobs"].values())


def test_deployment_only_after_successful_same_repository_main_push() -> None:
    deploy = workflow("deploy-dev.yml")
    assert deploy["on"] == {
        "workflow_run": {
            "workflows": ["CI"],
            "types": ["completed"],
            "branches": ["main"],
        }
    }
    job = deploy["jobs"]["deploy"]
    for condition in (
        "vars.AERA_DEV_DEPLOY_ENABLED == 'true'",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    ):
        assert condition in job["if"]
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert "environment" not in job  # Keep the branch subject, not an environment subject.
    assert job["env"]["AERA_ENV"] == "dev"
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.event.workflow_run.head_sha }}"
    assert checkout["with"]["persist-credentials"] == "false"
    assert job["steps"][-1]["run"] == "uv run --locked python scripts/deploy_ci.py"
