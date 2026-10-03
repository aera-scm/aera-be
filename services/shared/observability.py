"""Structured JSON logging with correlation ids on every line (NFR-OBS-01, SRD 6.22)."""

from __future__ import annotations

from aws_lambda_powertools import Logger

_loggers: dict[str, Logger] = {}


def get_logger(service: str) -> Logger:
    if service not in _loggers:
        _loggers[service] = Logger(service=f"aera-{service}", use_rfc3339=True)
    return _loggers[service]


def correlate(
    logger: Logger,
    *,
    case_id: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    """Attach caseId, runId and traceId to every following line of this logger."""
    logger.append_keys(caseId=case_id, runId=run_id, traceId=trace_id)
