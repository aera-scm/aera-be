"""Download and verify official SAP metadata inventory (IR-01, IR-02)."""

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

from sap_transport import APIS, BASE, HOST, MAX_BYTES, Fetch, SapError, environment_key, get

DIRECTORY = Path(__file__).resolve().parents[1] / "sap-mirror/metadata"
# Entity-set names required by SRD 6.6.2, not a substitute for EDMX field definitions.
ENTITY_SETS = {
    APIS[0]: {"A_PurchaseOrder", "A_PurchaseOrderItem", "A_PurchaseOrderScheduleLine"},
    APIS[1]: {"A_MatlStkInAcctMod"},
    APIS[2]: {"A_SalesOrderItem", "A_SalesOrderScheduleLine"},
    APIS[3]: {"A_ProductionOrder_2"},
    APIS[4]: {"A_Supplier", "A_BusinessPartner", "A_AddressEmailAddress"},
    APIS[5]: {"A_MaterialDocumentItem"},
}
EDMX = "http://schemas.microsoft.com/ado/2007/06/edmx"
EDM = "http://schemas.microsoft.com/ado/2008/09/edm"


def validate_edmx(body: bytes, api: str) -> None:
    try:
        # Reject DTDs even in UTF-16; no external entities or entity expansion.
        normalized = body.replace(b"\x00", b"").upper()
        if len(body) > MAX_BYTES or b"<!DOCTYPE" in normalized or b"<!ENTITY" in normalized:
            raise ValueError
        root = ElementTree.fromstring(body)
        if root.tag != f"{{{EDMX}}}Edmx" or root.get("Version") != "1.0":
            raise ValueError
        sets = {node.get("Name") for node in root.iter(f"{{{EDM}}}EntitySet")}
        if not ENTITY_SETS[api].issubset(sets):
            raise ValueError
    except (ValueError, ElementTree.ParseError):
        raise SapError("Invalid OData V2 EDMX or missing required entity sets.") from None


def download(directory: Path, key: str, *, fetch: Fetch = get) -> None:
    # Fetch and validate all six before touching the published inventory.
    bodies = {}
    for api in APIS:
        body = fetch(f"{BASE}/{api}/$metadata", key, "application/xml")
        validate_edmx(body, api)
        bodies[api] = body
    entries = []
    directory.mkdir(parents=True, exist_ok=True)
    for api, body in bodies.items():
        name = f"{api}.edmx"
        (directory / name).write_bytes(body)
        entries.append(
            {
                "api": api,
                "file": name,
                "sourceRef": f"https://{HOST}{BASE}/{api}/$metadata",
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    # Publish manifest last; an interrupted write fails the hash check.
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "retrievedAt": datetime.now(UTC).isoformat(),
                "method": "authenticated-https-get",
                "files": entries,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def check_inventory(directory: Path) -> int:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        entries = manifest["files"]
        if manifest["method"] != "authenticated-https-get" or len(entries) != len(APIS):
            raise ValueError
        if datetime.fromisoformat(manifest["retrievedAt"]).tzinfo is None:
            raise ValueError
        if {entry["api"] for entry in entries} != set(APIS):
            raise ValueError
        for entry in entries:
            api = entry["api"]
            if (
                entry["file"] != f"{api}.edmx"
                or entry["sourceRef"] != f"https://{HOST}{BASE}/{api}/$metadata"
            ):
                raise ValueError
            body = (directory / entry["file"]).read_bytes()
            if hashlib.sha256(body).hexdigest() != entry["sha256"]:
                raise ValueError
            validate_edmx(body, api)
    except (OSError, ValueError, KeyError, TypeError, SapError):
        raise SapError(
            "Official metadata inventory incomplete or invalid; complete OT-04."
        ) from None
    return len(APIS)


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--download", action="store_true")
    parser.add_argument("--directory", type=Path, default=DIRECTORY)
    args = parser.parse_args(argv)
    try:
        if args.download:
            download(args.directory, environment_key(os.environ if environ is None else environ))
        count = check_inventory(args.directory)
    except (SapError, OSError):
        print("Official metadata inventory incomplete or invalid; complete OT-04.", file=sys.stderr)
        return 1
    print(json.dumps({"status": "pass", "count": count, "sourceRef": "manifest.json"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
