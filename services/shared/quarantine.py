"""Quarantine a signal: record why, audit it, tell the console (FR-ING-04, FR-ING-05, UC-13)."""

from __future__ import annotations

from typing import Any

from services.shared.audit import AuditWriter
from services.shared.models import Signal, SignalStatus
from services.shared.runtime import emit
from services.shared.signals import SignalStore


def quarantine(
    signal: Signal,
    reason: str,
    *,
    signals: SignalStore,
    audit: AuditWriter,
    bus: Any,
    component: str,
    guardrail: str | None = None,
    env: str | None = None,
) -> Signal:
    update: dict[str, Any] = {"status": SignalStatus.QUARANTINED, "quarantine_reason": reason}
    if guardrail is not None:
        update["guardrail_result"] = guardrail
    quarantined = signal.model_copy(update=update)
    signals.save(quarantined)
    audit.record(
        f"SIGNAL#{signal.signal_id}",
        "SIGNAL_QUARANTINED",
        actor="system",
        payload={
            "signalId": signal.signal_id,
            "channel": signal.channel.value,
            "senderId": signal.sender_id,
            "reason": reason,
            "component": component,
        },
    )
    emit(
        bus,
        "SignalQuarantined",
        {"signalId": signal.signal_id, "reason": reason, "channel": signal.channel.value},
        component=component,
        environment=env,
    )
    return quarantined
