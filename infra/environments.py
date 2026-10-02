"""Environments a development checkout may deploy to (SRD 9.1).

``final`` is deployed only from a tagged release, so no entry point in this
repository accepts it.
"""

from urllib.parse import urlsplit

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


def console_origins(values: tuple[str, ...]) -> tuple[str, ...]:
    origins = []
    for value in values or ("http://localhost:5173",):
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("invalid console origin port") from None
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not parsed.hostname
            or parsed.scheme not in {"http", "https"}
            or (parsed.scheme == "http" and not local)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(c.isspace() for c in value)
            or port == 0
        ):
            raise ValueError("console origin must be HTTPS (HTTP allowed only for localhost)")
        origin = value.rstrip("/")
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)
