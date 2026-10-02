# Official metadata inventory (IR-02)

The six official EDMX files were downloaded from the SAP sandbox on 2026-10-02;
`manifest.json` records each source URL, the retrieval time and a SHA-256 hash.
No synthetic schema is stored here. Tests under `tests/fixtures/sap/` prove
transport behavior only.

To refresh the inventory, with the sandbox credential in `SAP_SANDBOX_API_KEY`, run:

```sh
uv run --locked python scripts/download_sap_metadata.py --download
uv run --locked python scripts/download_sap_metadata.py --check
uv run --locked python scripts/sap_sandbox_smoke.py
```

The downloader uses authenticated HTTPS GETs against the six sandbox `$metadata`
endpoints, rejects redirects, and writes exact bytes with source URLs, retrieval
time and SHA-256 hashes. Review official origin before committing the inventory;
hashes establish file integrity, not independent proof of provenance.
The check exits nonzero while any required official file or manifest is missing.
Portal-only downloads need a separately reviewed import path before use.
Neither script reads credential files or logs response contents.
