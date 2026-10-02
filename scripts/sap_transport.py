"""Bounded, read-only SAP sandbox transport (IR-01, NFR-SEC-03/05)."""

import http.client
import ssl
import zlib
from collections.abc import Callable, Mapping
from typing import Any

HOST = "sandbox.api.sap.com"
# SAP Gateway path of the sandbox; without it the sandbox answers 404.
BASE = "/s4hanacloud/sap/opu/odata/sap"
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
# The sandbox compresses successful responses; offer only what `_decoded` can undo.
ENCODINGS = "gzip, identity"
Fetch = Callable[[str, str, str], bytes]


class SapError(Exception):
    """Sanitized failure safe to show on the command line."""


def environment_key(environ: Mapping[str, str]) -> str:
    key = environ.get("SAP_SANDBOX_API_KEY", "")
    if not key or any(ord(char) < 32 or ord(char) > 126 for char in key):
        raise SapError(
            "SAP credential unavailable; set SAP_SANDBOX_API_KEY in the process environment."
        )
    return key


def _decoded(body: bytes, encoding: str) -> bytes:
    """Undo the sandbox's gzip transfer coding, with the same size bound as plain bodies."""
    encoding = encoding.strip().lower()
    if encoding in ("", "identity"):
        return body
    if encoding != "gzip":
        raise SapError("SAP response uses an unsupported content encoding.")
    try:
        decoder = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
        decoded = decoder.decompress(body, MAX_BYTES + 1)
    except zlib.error:
        raise SapError("SAP response could not be decoded.") from None
    if len(decoded) > MAX_BYTES or decoder.unconsumed_tail:
        raise SapError("SAP response exceeds the size limit.")
    if not decoder.eof:
        raise SapError("SAP response could not be decoded.")
    return decoded


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
        headers = {"APIKey": key, "Accept": accept, "Accept-Encoding": ENCODINGS}
        client.request("GET", path, headers=headers)
        response = client.getresponse()
        # No redirects: never forward the credential to another location.
        if response.status != 200:
            raise SapError(f"SAP request failed: HTTP {response.status}.")
        body: bytes = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise SapError("SAP response exceeds the size limit.")
        return _decoded(body, response.getheader("Content-Encoding", ""))
    except (OSError, http.client.HTTPException, ValueError):
        raise SapError("SAP transport failed (network, timeout or TLS).") from None
    finally:
        if client is not None:
            client.close()
