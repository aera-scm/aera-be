"""Scan repository candidates without reading local credential files (NFR-SEC-03)."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    # Lockfile integrity digests are not credentials; scan the manifests and source.
    candidates = [name for name in files if name not in {"uv.lock", "pnpm-lock.yaml"}]
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
