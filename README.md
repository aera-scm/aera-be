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
and transitive hashes are locked in `uv.lock`; CI rejects lockfile drift.
Pytest failures, collection errors and an empty collection fail the check.

The secret scan checks tracked and non-ignored candidate files, refuses credential
file paths without reading their contents, and disables credential verification
network calls. Lockfiles are excluded from secret detection because they contain
integrity digests; their dependencies are covered by the vulnerability audit.
Supply credentials only through runtime environment variables or Secrets Manager.

Dependency installation and vulnerability audits need public registry access;
installed lint, type, test and secret checks work without AWS. Run selected checks
with `uv run --locked python scripts/check.py lint typecheck test secrets`.
The SAP Mirror is not implemented yet. CI is validation
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

## Region and model checks

All data and processing stay in one region, `us-east-1` (NFR-CMP-02). Model access
must use direct regional inference (A-01): `MODEL_SUPERVISOR_ID` is a bare Anthropic
Claude model id and `MODEL_SMALL_ID` a bare Amazon Nova or Claude model id. Geographic
and global inference profiles (`us.`, `eu.`, `apac.`, `global.`, ...) and ARNs are
rejected, and every deployment command refuses any other region.

```sh
make check-region                       # offline: region configuration only
make check-models                       # offline: model id format and family
make check-region ARGS="--live"         # budget first, then one read-only list call
                                        # per service: Guardrails, Automated Reasoning,
                                        # AgentCore, Textract, Comprehend
make check-models ARGS="--live"         # budget first, then per model: on demand,
                                        # ACTIVE, authorized, agreement/entitlement
make check-models ARGS="--live --invoke"  # plus one Converse call, at most 8 tokens
```

Live checks need `AERA_AWS_PROFILE` and `AERA_BUDGET_NAME`. Offline results say
"not account evidence"; account checks and invocations print on their own labelled
lines. Without Make, run `uv run --locked python scripts/check_region.py` or
`scripts/check_model_access.py` with the same arguments.

## Data layer and configuration

`infra/app.py` is the main CDK app (`cdk.json` at the repository root). It holds the
`aera-{env}-data` stack, pinned to `us-east-1`:

- the ten DynamoDB tables of the data design (`aera-{env}-cases`, `-signals`, `-trace`,
  `-ledger`, `-idempotency`, `-audit`, `-dialogue`, `-analytics`, `-config`,
  `-connections`) with their keys, indexes, streams and `ttl` attributes; on-demand,
  point-in-time recovery, project KMS key, deletion protection;
- the `raw` (90-day expiry), `artefacts` and `audit` (Object Lock, compliance mode,
  90 days) buckets, named `aera-{env}-{component}-{account}-{region}`: KMS-encrypted,
  no public access, HTTPS with TLS 1.2 or later only;
- the project KMS key `alias/aera-{env}`, the `aera-{env}` event bus;
- SSM parameters `/aera/{env}/{KEY}` for model, Guardrail and SAP settings. Values not
  provisioned yet hold the literal `UNSET`, never a guessed id or URL;
- empty Secrets Manager containers `/aera/{env}/sap/sandbox-api-key` and
  `/aera/{env}/sap/mirror-oauth-client`.

Every resource is tagged `project`, `env`, `component` and `owner`; set `AERA_OWNER_TAG`.
Approved `MODEL_SUPERVISOR_ID` / `MODEL_SMALL_ID` are written to SSM when set, after the
same validation as `check-models`. To use an existing approved secret instead of a new
container, set `AERA_SAP_SANDBOX_SECRET_NAME` or `AERA_SAP_MIRROR_SECRET_NAME`.

```sh
make seed-config ENV=dev         # Config-table defaults; never overwrites existing values
make provision-secrets ENV=dev   # SAP_SANDBOX_API_KEY from your shell into the container
```

`provision-secrets` reads the key only from the `SAP_SANDBOX_API_KEY` variable of its
own process, never from arguments or files, and never prints it.

Built for the AWS / SAP Agentic AI Hackathon, track: Intelligent Supply Chain.
## SAP sandbox prerequisite

Read-only SAP transport and official metadata checks are described in
[`sap-mirror/metadata/README.md`](sap-mirror/metadata/README.md) (IR-01, IR-02).
Synthetic test success is not live sandbox or schema evidence.

## Identity and deployment foundations

The main CDK app synthesizes all nine stacks in SRD 6.16. Cognito has
planner/approver/admin groups and a public authorization-code client; no users
are created. The future browser client must use PKCE S256. Dev callbacks use
`http://localhost:5173/callback` and logout uses `http://localhost:5173/`.
The private `aera-dev-web` bucket is encrypted and requires TLS 1.2. CloudFront,
the console and runtime services remain deferred. Shells contain one unused
CloudFormation wait-condition handle (no wait condition) to make valid templates;
the handle URL is never output. The budget app remains independent.

### Optional GitHub OIDC (NFR-SEC-03/06)

OIDC resources are omitted unless `AERA_GITHUB_REPOSITORY` is explicitly set to
the approved `owner/repository`. Enabling it also requires `AERA_CDK_QUALIFIER`,
`AERA_BUDGET_NAME` and `AERA_GITHUB_PROVIDER_MODE` (`create` or `existing`). Keep
the provider mode unchanged after provisioning: changing it removes an owned
provider. Use `existing` only for a provider managed outside this stack.

Trust permits only `repo:<approved-repository>:ref:refs/heads/main` with audience
`sts.amazonaws.com`. The deployment role can assume only that qualifier's deploy,
file-publishing and lookup roles in the current account and approved region,
read the selected budget, and read the bootstrap version. It cannot bootstrap.
PR validation has no OIDC permission. No frontend role is provisioned yet.

Before enabling hosted deployment, the account owner must approve a dedicated dev
bootstrap qualifier and a customer CloudFormation execution policy restricted
to WP-0 resources in dev (including IAM role/provider operations), with no final
access and no AdministratorAccess. The policy is an owner-managed prerequisite;
its contents cannot be inferred from its ARN. Set its ARN locally as
`AERA_CFN_EXECUTION_POLICY_ARN`. The existing budget-gated bootstrap/deploy command
passes that policy and qualifier to CDK. Local profile inputs stay private.
Do not enable hosted deployment until the actual bootstrap roles and execution
policy have been reviewed in the account.

Configure repository variables `AERA_REGION=us-east-1`, `AERA_OWNER_TAG`,
`AERA_GITHUB_REPOSITORY`, `AERA_CDK_QUALIFIER`, `AERA_GITHUB_PROVIDER_MODE`,
`AERA_BUDGET_NAME`, `AERA_BUDGET_LIMIT_USD` and `AERA_DEV_DEPLOY_ROLE_ARN`.
Preserve any approved model IDs and existing-secret names with the corresponding
`MODEL_*` and `AERA_SAP_*_SECRET_NAME` variables. These are names/IDs, never keys.
Set `AERA_DEV_DEPLOY_ENABLED=true` only after initial local provisioning succeeds.

`deploy-dev.yml` requires a successful CI run from a push to main in the same
repository and checks out that exact tested commit. It gets temporary OIDC
credentials, verifies the real budget, then deploys the existing foundations.
It does not bootstrap, provision SAP key values, seed business data, or deploy a
frontend bundle. Hosted CI, cloud deployment and live SAP evidence remain pending.
