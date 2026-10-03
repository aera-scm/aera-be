"""Domain events on the environment bus `aera-{env}` in the shared envelope (SRD 6.17)."""

from __future__ import annotations

from typing import Any

from services.shared.models import EventEnvelope


class EventPublishError(Exception):
    pass


def publish(client: Any, envelope: EventEnvelope, *, component: str) -> None:
    result = client.put_events(
        Entries=[
            {
                "EventBusName": f"aera-{envelope.env}",
                "Source": f"aera.{component}",
                "DetailType": envelope.type,
                "Detail": envelope.model_dump_json(by_alias=True),
            }
        ]
    )
    if result.get("FailedEntryCount"):
        codes = ", ".join(entry.get("ErrorCode", "?") for entry in result.get("Entries", []))
        raise EventPublishError(f"{envelope.type} was not accepted: {codes}")
