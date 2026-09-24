"""Supplier and carrier contacts from SAP business partner master data (BR-04, IR-02).

Contacts are read through the SAP client (so from the Mirror, the sandbox or a tenant alike)
and cached for a short time; a partner whose grouping is `CARR` is a carrier, every other
partner with a supplier number is a supplier.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from services.rules.br_04 import PartnerContact
from services.shared.sap_client import SapClient

SERVICE = "API_BUSINESS_PARTNER"
CARRIER_GROUPING = "CARR"
CACHE_SECONDS = 300.0


def load_contacts(sap: SapClient) -> list[PartnerContact]:
    partners = sap.query(
        SERVICE,
        "A_BusinessPartner",
        select="BusinessPartner,Supplier,BusinessPartnerGrouping",
        top=1000,
    )
    addresses = sap.query(
        SERVICE, "A_BusinessPartnerAddress", select="BusinessPartner,AddressID", top=1000
    )
    emails = sap.query(SERVICE, "A_AddressEmailAddress", select="AddressID,EmailAddress", top=1000)
    phones = sap.query(
        SERVICE, "A_AddressPhoneNumber", select="AddressID,InternationalPhoneNumber", top=1000
    )
    address_of: dict[str, set[str]] = {}
    for row in addresses:
        address_of.setdefault(str(row.data["BusinessPartner"]), set()).add(
            str(row.data["AddressID"])
        )
    domains: dict[str, set[str]] = {}
    for row in emails:
        address = str(row.data.get("EmailAddress") or "")
        if address.count("@") == 1:
            domains.setdefault(str(row.data["AddressID"]), set()).add(
                address.split("@")[1].strip().lower()
            )
    numbers: dict[str, set[str]] = {}
    for row in phones:
        number = str(row.data.get("InternationalPhoneNumber") or "").replace(" ", "")
        if number:
            numbers.setdefault(str(row.data["AddressID"]), set()).add(number)

    contacts: list[PartnerContact] = []
    for row in partners:
        partner = str(row.data["BusinessPartner"])
        carrier = row.data.get("BusinessPartnerGrouping") == CARRIER_GROUPING
        if not carrier and not row.data.get("Supplier"):
            continue
        ids = address_of.get(partner, set())
        contacts.append(
            PartnerContact(
                partner_id=str(row.data.get("Supplier") or partner),
                kind="CARRIER" if carrier else "SUPPLIER",
                email_domains=frozenset(d for a in ids for d in domains.get(a, ())),
                phones=frozenset(p for a in ids for p in numbers.get(a, ())),
            )
        )
    return contacts


@dataclass
class ContactDirectory:
    sap: SapClient
    clock: Callable[[], float] = time.monotonic
    _cached: tuple[float, list[PartnerContact]] | None = field(default=None, init=False)

    def contacts(self) -> list[PartnerContact]:
        if self._cached is None or self.clock() - self._cached[0] >= CACHE_SECONDS:
            self._cached = (self.clock(), load_contacts(self.sap))
        return self._cached[1]
