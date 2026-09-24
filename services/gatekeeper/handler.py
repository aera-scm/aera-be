"""Gatekeeper: nothing reaches a case or the agent unless the sender is known to SAP and the
text passes the prompt-attack scan (SRD 6.25.1 step 2, FR-ING-04, FR-ING-05, BR-04, UC-13).

On `SignalReceived`:

1. Sender check against SAP business partner master data (BR-04). An email whose SES
   verdicts show both SPF and DKIM failing is refused too: its From header proves nothing.
2. Text collection: the signal text plus everything hidden (HTML hidden elements, comments,
   attributes; every PDF text layer and metadata).
3. Amazon Bedrock Guardrails `ApplyGuardrail` (prompt-attack filter, HIGH) over all of it.

A failure quarantines the signal with its reason, writes an audit event and emits
`SignalQuarantined`; the signal is never attached to a case. Otherwise it is ACCEPTED with
the matched partner and `SignalAccepted` is emitted.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from services.rules.br_04 import PartnerContact, rejection_reason, verify_sender
from services.shared.audit import AuditWriter
from services.shared.models import Signal, SignalChannel, SignalStatus
from services.shared.quarantine import quarantine
from services.shared.runtime import emit
from services.shared.signals import RawStore, SignalStore
from services.shared.text import html_texts, pdf_text

COMPONENT = "gatekeeper"
CHUNK = 10_000
OVERLAP = 200  # so a phrase cut at a chunk boundary is still seen whole


@dataclass(frozen=True)
class Scan:
    blocked: bool
    reason: str | None = None


class Guardrail:
    """ApplyGuardrail on INPUT text, in chunks; any intervention blocks the signal."""

    def __init__(self, client: Any, identifier: Callable[[], str], version: Callable[[], str]):
        self._client = client
        self._identifier = identifier
        self._version = version

    def scan(self, text: str) -> Scan:
        for start in range(0, max(len(text), 1), CHUNK - OVERLAP):
            chunk = text[start : start + CHUNK]
            if not chunk.strip():
                continue
            result = self._client.apply_guardrail(
                guardrailIdentifier=self._identifier(),
                guardrailVersion=self._version(),
                source="INPUT",
                content=[{"text": {"text": chunk}}],
            )
            if result.get("action") == "GUARDRAIL_INTERVENED":
                return Scan(True, _intervention(result))
        return Scan(False)


def _intervention(result: dict[str, Any]) -> str:
    found = []
    for assessment in result.get("assessments") or []:
        for item in (assessment.get("contentPolicy") or {}).get("filters") or []:
            if item.get("action", "BLOCKED") == "BLOCKED":
                found.append(f"{item.get('type', 'UNKNOWN')} ({item.get('confidence', '?')})")
    detail = ", ".join(found) or "policy intervention"
    return f"Guardrail blocked the text: {detail}"


def _email_auth_failed(raw: bytes) -> bool:
    headers = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0].decode("latin-1").lower()
    results = " ".join(re.findall(r"^authentication-results:.*(?:\n[ \t].*)*", headers, re.M))
    if not results:
        return False
    return bool(re.search(r"\bspf=fail", results)) and bool(re.search(r"\bdkim=fail", results))


def _sender_channel(signal: Signal) -> str:
    if signal.channel in (SignalChannel.EMAIL, SignalChannel.WHATSAPP, SignalChannel.CARRIER):
        return signal.channel.value
    # Manual uploads, lab scenarios and agent messages name the real sender they carry.
    if "@" in signal.sender_id:
        return "EMAIL"
    if re.fullmatch(r"\+?[0-9 ()-]{7,20}", signal.sender_id):
        return "WHATSAPP"
    return "CARRIER"


@dataclass
class Gatekeeper:
    signals: SignalStore
    raw: RawStore
    contacts: Callable[[], list[PartnerContact]]
    guardrail: Guardrail
    audit: AuditWriter
    bus: Any
    env: str | None = None

    def handle(self, signal_id: str) -> Signal | None:
        signal = self.signals.get(signal_id)
        if signal is None or signal.status is not SignalStatus.RECEIVED:
            return signal  # unknown or already decided: redelivery is a no-op
        raw = self.raw.get(signal.raw_s3_key)

        channel = _sender_channel(signal)
        known = self.contacts()
        partner = verify_sender(channel, signal.sender_id, known)
        if partner is None:
            return self._quarantine(signal, rejection_reason(channel, signal.sender_id, known))
        if signal.channel is SignalChannel.EMAIL and _email_auth_failed(raw):
            return self._quarantine(signal, "email failed both SPF and DKIM; sender is unproven")

        visible, hidden = self._texts(signal, raw)
        scan = self.guardrail.scan("\n\n".join(t for t in (visible, hidden) if t))
        if scan.blocked:
            where = " (including hidden text)" if hidden else ""
            return self._quarantine(signal, f"{scan.reason}{where}", guardrail="BLOCKED")

        accepted = signal.model_copy(
            update={
                "status": SignalStatus.ACCEPTED,
                "sender_verified": True,
                "supplier_id": partner.partner_id,
                "guardrail_result": "PASSED",
            }
        )
        self.signals.save(accepted)
        emit(
            self.bus,
            "SignalAccepted",
            {"signalId": signal.signal_id, "partnerId": partner.partner_id, "kind": partner.kind},
            component=COMPONENT,
            environment=self.env,
        )
        return accepted

    def _texts(self, signal: Signal, raw: bytes) -> tuple[str, str]:
        visible = signal.normalized_text or ""
        hidden: list[str] = []
        if signal.channel is SignalChannel.EMAIL:
            from services.ses_inbound.handler import parse

            for part in parse(raw).walk():
                if part.get_content_type() == "text/html":
                    hidden.append(html_texts(str(part.get_content()))[1])
        for key in signal.attachments:
            if key.lower().endswith(".pdf"):
                hidden.append(pdf_text(self.raw.get(key)))
        return visible, "\n".join(h for h in hidden if h)

    def _quarantine(self, signal: Signal, reason: str, *, guardrail: str | None = None) -> Signal:
        return quarantine(
            signal,
            reason,
            signals=self.signals,
            audit=self.audit,
            bus=self.bus,
            component=COMPONENT,
            guardrail=guardrail,
            env=self.env,
        )


_gatekeeper: Gatekeeper | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _gatekeeper
    if _gatekeeper is None:
        from services.shared import runtime
        from services.shared.partners import ContactDirectory

        dynamodb = runtime.client("dynamodb")
        directory = ContactDirectory(runtime.sap_client())
        _gatekeeper = Gatekeeper(
            signals=SignalStore(dynamodb),
            raw=RawStore(runtime.client("s3"), runtime.raw_bucket()),
            contacts=directory.contacts,
            guardrail=Guardrail(
                runtime.client("bedrock-runtime"),
                lambda: runtime.parameter("GUARDRAIL_ID"),
                lambda: runtime.parameter("GUARDRAIL_VERSION"),
            ),
            audit=AuditWriter(dynamodb),
            bus=runtime.client("events"),
        )
    signal = _gatekeeper.handle(str(event["detail"]["data"]["signalId"]))
    return {"status": None if signal is None else signal.status.value}
