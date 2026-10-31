# Development

## Setup

Python 3.12 and uv are pinned for the backend; Node 24 and pnpm for the CDK CLI and the SAP
Mirror.

```sh
uv sync --locked
pnpm install --frozen-lockfile
pnpm --dir sap-mirror install --frozen-lockfile
```

With GNU Make, `make setup` runs the same commands. The direct commands also work on Windows
without Make.

## Checks

```sh
uv run --locked python scripts/check.py                       # everything CI runs
uv run --locked python scripts/check.py lint typecheck test   # a selection
uv run --locked pre-commit run --all-files
```

`scripts/check.py` runs Ruff lint and format, strict mypy, YAML validation, pytest (including
CDK synth assertions), a secret scan and a dependency vulnerability audit. Package versions and
transitive hashes are locked in `uv.lock` and `pnpm-lock.yaml`; CI rejects lockfile drift.
Pytest failures, collection errors and an empty collection all fail the check.

The secret scan checks tracked and non-ignored candidate files, refuses credential file paths
without reading their contents and never verifies credentials over the network. Lockfiles are
excluded from secret detection (they contain integrity digests) and are covered by the
vulnerability audit instead.

Installed lint, type, test and secret checks need no AWS account; installation and audits
need public registry access.

## Tests

| Suite | Where | What it covers |
|---|---|---|
| Unit and integration | `services/*/tests`, `tests/` | Every business rule (`test_br_xx_...`) and verifier check (`test_v_xx_...`), tools, case lifecycle, routing, execution workflow, API, realtime |
| Infrastructure | `infra/tests` | CDK synth assertions: IAM boundaries, encryption, event wiring, naming, tags |
| SAP Mirror | `sap-mirror/test` | OData contract, CSRF and ETag handling, deep insert, reset; schema against the official EDMX when present |
| Agent | `services/agent/tests` | Prompt contract, run limits, hooks, scripted reference runs |

At the end of the project the backend suite had **1,399 passing tests**:

```sh
uv run --locked pytest -q
```

## Evaluation harness

`eval/` holds 150 scored cases:

| Group | Cases |
|---|---|
| Late PO, clear | 25 |
| Late PO, competing for the same stock | 10 |
| Material shortage | 25 |
| Carrier delay | 15 |
| Low-confidence input (must escalate) | 15 |
| No viable option (must escalate) | 15 |
| Multilingual (English, Bahasa Indonesia, German) | 15 |
| Adversarial (hidden instructions, zero-size fonts, look-alike senders, plans with outside recipients) | 30 |

For each case the runner resets the Mirror, applies the case's changes, delivers the signals
through the real pipeline (intake, gatekeeper, extraction, case service), calls the real tools,
runs the verifier and routing, and scores the outcome deterministically.

```sh
uv run --locked python eval/runner.py --subset ci --out eval/reports   # what CI runs
make eval                                                              # full set
```

Offline, the case's recorded plan stands in for the model and Guardrail stand-ins replace
Bedrock. What depends on the model itself — its plan choice, tool-call accuracy, time to plan
and cost per case — is reported as *not measured* until the runner is pointed at a deployed
agent.

## Running pieces locally

```sh
make mirror-local                          # SAP Mirror with the reference scenario, port 4004
make agent-local ENV=dev CASE=EXC-2026-0914 # run the supervisor on one case from your machine
make types FE=../aera-fe                   # regenerate the console's API types from the schemas
```

`agent-local` uses the same prompt, hooks and tools as the deployed runtime against the
environment's tables and SAP, so a local run looks identical in the trace.

## API contract

The console API's request and response models are Pydantic models in `services/shared` and
`services/api`. `scripts/export_schemas.py` exports them as JSON Schema and
`scripts/generate-types.mjs` turns that into `src/api/types.generated.ts` in the console, so
both sides change together.

## Conventions

- Commit messages: `type(scope): summary [IDs]`, for example
  `feat(verifier): compliance gate [V-06]`.
- Every business rule (`BR-xx`) and check (`V-xx`) is deterministic code with a unit test named
  after its ID. No rule lives only in a prompt.
- Every figure shown to a user or written to SAP carries a `sourceRef`.
- Secrets come only from AWS Secrets Manager or runtime environment variables; only
  `.env.example` with fake values belongs in the repository.

## CI

`.github/workflows/ci.yml` runs `scripts/check.py` on every push and pull request, with
read-only repository permissions and no cloud credentials. `deploy-dev.yml` (optional,
disabled by default) deploys `main` after a green CI run — see
[DEPLOYMENT.md](DEPLOYMENT.md#7-hosted-deployment-from-github-optional).
