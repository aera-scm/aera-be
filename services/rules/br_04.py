"""BR-04 / FR-ING-04: accept a signal only from a supplier or carrier known to SAP master data.

Domains match exactly (no subdomains, no look-alikes); phone numbers match in E.164 form."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from email.utils import parseaddr
from typing import Literal

PartnerKind = Literal["SUPPLIER", "CARRIER"]
LOOK_ALIKE_RATIO = 0.8
_DOMAIN = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")
_PHONE_NOISE = re.compile(r"[ ().-]")
_PHONE = re.compile(r"[1-9][0-9]{6,14}")


@dataclass(frozen=True)
class PartnerContact:
    partner_id: str
    kind: PartnerKind
    email_domains: frozenset[str]
    phones: frozenset[str]


def email_domain(sender: str) -> str | None:
    if sender.count("@") != 1:
        return None
    address = parseaddr(sender)[1]
    if address.count("@") != 1:
        return None
    local, domain = address.split("@")
    domain = domain.strip().lower().rstrip(".")
    if not local or not _DOMAIN.fullmatch(domain):
        return None
    return domain


def e164(sender: str) -> str | None:
    digits = _PHONE_NOISE.sub("", sender.strip().removeprefix("whatsapp:")).removeprefix("+")
    return f"+{digits}" if _PHONE.fullmatch(digits) else None


def verify_sender(
    channel: str, sender: str, contacts: Iterable[PartnerContact]
) -> PartnerContact | None:
    if channel == "EMAIL":
        domain = email_domain(sender)
        return next((c for c in contacts if domain is not None and domain in c.email_domains), None)
    if channel == "WHATSAPP":
        phone = e164(sender)
        return next((c for c in contacts if phone is not None and phone in c.phones), None)
    if channel == "CARRIER":
        return next(
            (c for c in contacts if c.kind == "CARRIER" and c.partner_id == sender.strip()), None
        )
    return None


def rejection_reason(channel: str, sender: str, contacts: Iterable[PartnerContact]) -> str:
    known = list(contacts)
    if verify_sender(channel, sender, known) is not None:
        raise ValueError("sender is verified")
    if channel == "EMAIL":
        domain = email_domain(sender)
        if domain is None:
            return "sender address is not a valid email address"
        reason = f"sender domain {domain} is not in SAP master data"
        closest = max(
            (d for c in known for d in c.email_domains),
            key=lambda d: SequenceMatcher(None, d, domain).ratio(),
            default=None,
        )
        if closest and SequenceMatcher(None, closest, domain).ratio() >= LOOK_ALIKE_RATIO:
            reason += f"; it resembles the registered domain {closest}"
        return reason
    if channel == "WHATSAPP":
        return "sender phone number is not registered in SAP master data"
    if channel == "CARRIER":
        return f"carrier {sender.strip()} is not a carrier in SAP master data"
    return f"channel {channel} has no sender verification"
