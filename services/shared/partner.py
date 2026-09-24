"""Read SAP business-partner email addresses for allowlisted recipients."""

from services.shared.sap_client import SapClient

PARTNER = "API_BUSINESS_PARTNER"


def partner_emails(sap: SapClient, partner: str) -> set[str]:
    addresses = sap.query(
        PARTNER,
        "A_BusinessPartnerAddress",
        filter=f"BusinessPartner eq '{partner}'",
        select="AddressID",
    )
    emails: set[str] = set()
    for address in addresses:
        rows = sap.query(
            PARTNER,
            "A_AddressEmailAddress",
            filter=f"AddressID eq '{address.data['AddressID']}'",
            select="EmailAddress",
        )
        emails |= {str(r.data["EmailAddress"]).strip().lower() for r in rows}
    return emails
