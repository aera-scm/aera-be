"""Environments a development checkout may deploy to (SRD 9.1).

``final`` is deployed only from a tagged release, so no entry point in this
repository accepts it.
"""

DEPLOYABLE_ENVIRONMENTS: frozenset[str] = frozenset({"dev"})
# ADR-001 / NFR-CMP-02: one region for everything. Mirrors scripts/check_region.py;
# a test keeps the two equal.
APPROVED_REGION = "us-east-1"


class EnvironmentRefusedError(ValueError):
    """Raised when a command targets an environment it may not deploy to."""


def require_deployable_environment(name: str) -> str:
    if name not in DEPLOYABLE_ENVIRONMENTS:
        raise EnvironmentRefusedError(
            f"Environment {name!r} cannot be deployed from here; only 'dev' is allowed."
        )
    return name
