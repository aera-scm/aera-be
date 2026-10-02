"""Which files the secret scan reads (NFR-SEC-03)."""

import security
from sap_transport import APIS


def test_nfr_sec_03_scan_skips_only_integrity_digests_and_the_official_inventory() -> None:
    inventory = [f"sap-mirror/metadata/{api}.edmx" for api in APIS]
    skipped = ["uv.lock", "pnpm-lock.yaml", "sap-mirror/metadata/manifest.json", *inventory]
    scanned = [
        "scripts/security.py",
        "sap-mirror/metadata/README.md",
        "sap-mirror/metadata/notes.edmx",
        "sap-mirror/metadata/API_UNLISTED_SRV.edmx",
        "sap-mirror/other/API_BUSINESS_PARTNER.edmx",
        "other/manifest.json",
        "sap-mirror/pnpm-lock.yaml",
    ]

    assert len(inventory) == 6
    assert security.scan_candidates([*skipped, *scanned]) == scanned
