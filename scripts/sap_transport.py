"""Bounded, read-only SAP sandbox transport (IR-01, NFR-SEC-03/05)."""

import http.client
import ssl
from collections.abc import Callable, Mapping
from typing import Any

HOST = "sandbox.api.sap.com"
BASE = "/s4hanacloud"
APIS = (
    "API_PURCHASEORDER_PROCESS_SRV",
    "API_MATERIAL_STOCK_SRV",
    "API_SALES_ORDER_SRV",
    "API_PRODUCTION_ORDER_2_SRV",
    "API_BUSINESS_PARTNER",
    "API_MATERIAL_DOCUMENT_SRV",
)
PO_PATH = f"{BASE}/{APIS[0]}/A_PurchaseOrder?$top=5"
PATHS = frozenset([PO_PATH, *(f"{BASE}/{api}/$metadata" for api in APIS)])
MAX_BYTES = 16 * 1024 * 1024
Fetch = Callable[[str, str, str], bytes]


class SapError(Exception):
    """Sanitized failure safe to show on the command line."""


def environment_key(environ: Mapping[str, str]) -> str:
    key = environ.get("SAP_SANDBOX_API_KEY", "")
    if not key or any(ord(char) < 32 or ord(char) > 126 for char in key):
        raise SapError("SAP credential unavailable; complete OT-04 using the process environment.")
    return key


def get(
    path: str,
    key: str,
    accept: str,
    *,
    connection: Callable[..., Any] = http.client.HTTPSConnection,
) -> bytes:
    if path not in PATHS:
        raise SapError("Unapproved SAP path.")
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    client = None
    try:
        client = connection(HOST, timeout=20, context=context)
        client.request("GET", path, headers={"APIKey": key, "Accept": accept})
        response = client.getresponse()
        # No redirects: never forward the credential to another location.
        if response.status != 200:
            raise SapError(f"SAP request failed: HTTP {response.status}.")
        body: bytes = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise SapError("SAP response exceeds the size limit.")
        return body
    except (OSError, http.client.HTTPException, ValueError):
        raise SapError("SAP transport failed (network, timeout or TLS).") from None
    finally:
        if client is not None:
            client.close()
