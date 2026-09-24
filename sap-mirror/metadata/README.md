# Official metadata prerequisite (IR-02)

The six official EDMX files are not yet available. No synthetic schema is stored
here. Tests under `tests/fixtures/sap/` prove transport behavior only.

Once the sandbox credential is available in `SAP_SANDBOX_API_KEY`, run:

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
