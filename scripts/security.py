"""Scan repository candidates without reading local credential files (NFR-SEC-03)."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Not credentials: lockfile integrity digests, and the official SAP metadata inventory.
# Its manifest holds SHA-256 hashes, and its schemas are SAP's published XML, whose bytes
# `download_sap_metadata.py --check` pins to those hashes. A test keeps this list exact.
UNSCANNED = frozenset(
    {
        "uv.lock",
        "pnpm-lock.yaml",
        "sap-mirror/metadata/manifest.json",
        "sap-mirror/metadata/API_PURCHASEORDER_PROCESS_SRV.edmx",
        "sap-mirror/metadata/API_MATERIAL_STOCK_SRV.edmx",
        "sap-mirror/metadata/API_SALES_ORDER_SRV.edmx",
        "sap-mirror/metadata/API_PRODUCTION_ORDER_2_SRV.edmx",
        "sap-mirror/metadata/API_BUSINESS_PARTNER.edmx",
        "sap-mirror/metadata/API_MATERIAL_DOCUMENT_SRV.edmx",
    }
)


def scan_candidates(files: list[str]) -> list[str]:
    return [name for name in files if name not in UNSCANNED]


def repository_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return sorted(set(result.stdout.decode("utf-8").rstrip("\0").split("\0")) - {""})


def main() -> int:
    files = repository_files()
    forbidden = [
        name
        for name in files
        if (
            (Path(name).name.startswith(".env") and Path(name).name != ".env.example")
            or Path(name).suffix.lower() in {".pem", ".key"}
            or "secrets" in Path(name).parts
        )
    ]
    if forbidden:
        print("Credential files are repository candidates; remove them without printing contents.")
        for name in forbidden:
            print(name)
        return 1
    candidates = scan_candidates(files)
    result = subprocess.run(
        ["detect-secrets", "scan", "--no-verify", "--all-files", *candidates],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(result.stdout)
    findings = report["results"]
    if findings:
        print("Secret scan failed. Values are withheld; inspect these files privately:")
        for name, entries in findings.items():
            for entry in entries:
                print(f"{name}:{entry['line_number']} ({entry['type']})")
        return 1
    print(f"Secret scan passed: {len(candidates)} files; no credential verification requests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
