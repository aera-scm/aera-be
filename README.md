# AERA — backend, agent and infrastructure

AERA (Autonomous Exception Resolution Agent) takes a supply chain exception from a raw external signal — a supplier email, a PDF order confirmation, a photographed confirmation sent by WhatsApp, or a carrier notice — through to a verified, approved and reversible correction in SAP S/4HANA.

This repository holds the backend: the reasoning agent, the deterministic control plane, the infrastructure, the SAP Mirror service and the evaluation harness. The web console lives in `aera-fe`.

## What it does

- **Reads what planners read:** supplier email and PDFs, WhatsApp photos, carrier events, SAP MRP exceptions — in English, Bahasa Indonesia and German.
- **Grounds every number in SAP.** Figures are fetched, never invented. Where a message and SAP disagree, SAP wins.
- **Quantifies the impact:** which customer orders are exposed, when stock runs out, how much revenue is at risk.
- **Proposes options** (inter-plant transfer, expedite, PO re-schedule, alternate supplier) with cost, arrival date and a time-phased stock projection, and optimises across competing cases.
- **Decides how much to automate:** small, reversible, high-confidence actions run automatically with sampled audit; large or irreversible ones need a named approver.
- **Executes safely:** deterministic workflow, stock reservation, undo plan saved before the first write, idempotent writes, and the case reopens itself if the goods never arrive.
- **Asks the supplier directly** when a fact is missing, within strict messaging rules.

## Architecture

Reasoning runs on Amazon Bedrock AgentCore with the Strands Agents SDK and has read-only tools. Verification, routing, stock reservation and all writes are deterministic services: AWS Step Functions, Lambda, DynamoDB, EventBridge. Document and language understanding use Amazon Textract and Comprehend; safety uses Amazon Bedrock Guardrails. SAP S/4HANA Cloud APIs are the system of record, with an SAP CAP service on SAP BTP for writes.

## Layout

```text
infra/         AWS CDK stacks
services/      shared, rules, ingestion, agent, tools, verifier, routing, optimizer,
               workflow, dialogue, analytics, lab, interop, monitor, api
sap-mirror/    SAP CAP service with the S/4HANA schema
data/synthetic/ generators for test signals
eval/          evaluation harness and cases
```

## Getting started

Python 3.12.14 and uv 0.12.18 are pinned for the foundation tooling. Install these
before running the following commands from this repository:

```sh
uv sync --locked
uv run --locked python scripts/check.py
uv run --locked pre-commit run --all-files
```

With GNU Make, `make setup`, `make check` and `make hooks` run the same commands.
The direct commands also work on Windows without Make. Keep existing Git hooks:
run pre-commit explicitly rather than replacing an existing `core.hooksPath`.

Checks cover Ruff lint/format, strict mypy, YAML, pytest when tests exist, a secret
scan and a dependency vulnerability audit (NFR-SEC-03, NFR-SEC-06). Package versions
and transitive hashes are locked in `uv.lock`; CI rejects lockfile drift. The test
command currently reports that no unit/contract tests exist. Once tests are added,
pytest failures, collection errors and an empty collection fail the check.

The secret scan checks tracked and non-ignored candidate files, refuses credential
file paths without reading their contents, and disables credential verification
network calls. Lockfiles are excluded from secret detection because they contain
integrity digests; their dependencies are covered by the vulnerability audit.
Supply credentials only through runtime environment variables or Secrets Manager.

Dependency installation and vulnerability audits need public registry access;
installed lint, type, test and secret checks work without AWS. Run selected checks
with `uv run --locked python scripts/check.py lint typecheck test secrets`.
The Mirror and the main infrastructure app are not implemented yet. CI is validation
only, with read-only repository permissions and no deployment credentials.

## Budget gate

An AWS Budget with actual-spend alerts at 50, 80 and 100% must exist before any
other resource, including those created by `cdk bootstrap` (NFR-COST-01, C-02).
`infra/budget_app.py` is a standalone CDK app holding one native budget resource;
it deploys with CLI credentials and needs no bootstrap. The CDK CLI is pinned in
`package.json` and installed with `pnpm install --frozen-lockfile` (Node 24.21.0).

Inputs come from the environment, never from committed files:

| Variable | Meaning |
|---|---|
| `AERA_AWS_PROFILE` | named AWS CLI profile |
| `AERA_REGION` | deployment region (default `us-east-1`) |
| `AERA_BUDGET_NAME` | budget name |
| `AERA_BUDGET_LIMIT_USD` | approved monthly amount in USD |
| `AERA_BUDGET_RECIPIENTS` | comma-separated alert email addresses (budget creation only) |

```sh
make budget ENV=dev        # deploy the budget stack, then verify it
make check-budget          # verify amount, period, thresholds and subscribers
make bootstrap ENV=dev     # verify the budget, then cdk bootstrap
make deploy ENV=dev        # verify the budget, cdk bootstrap, then cdk deploy --all
```

Without Make, run `uv run --locked python scripts/deploy_dev.py <action> --env dev`.
If an approved budget already exists, skip `make budget`; bootstrap and deploy verify
the existing budget by name and never change it. A failed verification stops before
any bootstrap or CloudFormation call. Only `dev` is accepted; `final` is deployed
from a tagged release, never from a development checkout.

Built for the AWS / SAP Agentic AI Hackathon, track: Intelligent Supply Chain.
