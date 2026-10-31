# Deployment

AERA deploys to one AWS account and region with the AWS CDK. This guide covers a `dev`
environment; a `final` environment is deployed only from a tagged release
([final-runbook.md](final-runbook.md)).

## What you need

| Requirement | Notes |
|---|---|
| AWS account | Amazon Bedrock access to an Anthropic Claude model and Amazon Nova Lite in `us-east-1`; AgentCore, Textract, Comprehend and Guardrails available in the region |
| Tooling | Python 3.12 + uv, Node 24 + pnpm (CDK CLI is pinned in `package.json`), Docker with ARM64 build support (agent container), GNU Make optional |
| SAP | Optional: SAP Business Accelerator Hub API key for the read-only sandbox checks, or SAP BTP for the Mirror. Without BTP the Mirror runs on ECS Fargate. |
| Channels | Optional: WhatsApp Business Cloud API test number, an SES-verified domain for inbound email. Without them, signals are replayed with `scripts/replay_signals.py`. |

**Cost.** Idle cost of a dev environment is about USD 1 per day (Cognito M2M tokens, the Mirror
Fargate task, public IPv4, Secrets Manager and KMS). Agent runs cost cents each; most of the
hackathon spend was model inference. AgentCore has no free tier.

## 1. Budget gate

An AWS Budget with actual-spend alerts at 50, 80 and 100 % must exist before any other
resource, including those created by `cdk bootstrap`. `infra/budget_app.py` is a standalone
CDK app holding one native budget resource; it deploys with CLI credentials and needs no
bootstrap.

| Variable | Meaning |
|---|---|
| `AERA_AWS_PROFILE` | named AWS CLI profile |
| `AERA_REGION` | deployment region (default `us-east-1`) |
| `AERA_BUDGET_NAME` | budget name |
| `AERA_BUDGET_LIMIT_USD` | monthly amount in USD |
| `AERA_BUDGET_RECIPIENTS` | comma-separated alert email addresses (creation only) |

```sh
make budget ENV=dev        # deploy the budget stack, then verify it
make check-budget          # verify amount, period, thresholds and subscribers
```

Without Make: `uv run --locked python scripts/deploy_dev.py <action> --env dev`. If a budget
already exists, skip `make budget`; bootstrap and deploy verify it by name and never change it.

## 2. Region and model checks

All data stays in one region, `us-east-1`. `MODEL_SMALL_ID` is a bare Amazon Nova or Claude
model id used by direct regional inference. `MODEL_SUPERVISOR_ID` is an Anthropic Claude model
id: a bare regional id or the US geographic inference profile (`us.anthropic.claude-...`).
Other geographic or global profiles are rejected, and every deploy command refuses any other
region.

```sh
make check-region                         # offline: region configuration
make check-models                         # offline: model id format and family
make check-region ARGS="--live"           # one read-only list call per service
make check-models ARGS="--live --invoke"  # model access plus one Converse call (≤ 8 tokens)
```

## 3. Configure and deploy

Inputs come from the environment, never from committed files. A typical dev deploy:

```sh
export AERA_AWS_PROFILE=<profile> AERA_REGION=us-east-1 AERA_ENV=dev \
  AERA_OWNER_TAG=<your-name> AERA_BUDGET_NAME=<budget> AERA_BUDGET_LIMIT_USD=100 \
  MODEL_SUPERVISOR_ID=us.anthropic.claude-sonnet-4-6 MODEL_SMALL_ID=amazon.nova-lite-v1:0 \
  AERA_MIRROR_HOSTING=ecs AERA_WHATSAPP_MEDIA=replay \
  AERA_CONSOLE_ORIGINS="http://localhost:5173,https://<your-cloudfront-domain>"

make lambda-bundle                 # build the Lambda bundle (build/lambda)
make bootstrap ENV=dev             # verify the budget, then cdk bootstrap
make deploy ENV=dev                # verify the budget, then cdk deploy --all
make seed-config ENV=dev           # Config-table defaults: thresholds, rate card, approvers (never overwrites)
```

- `AERA_CONSOLE_ORIGINS` lists every origin the console is served from; Cognito callbacks and
  API CORS are generated from it.
- `AERA_MIRROR_HOSTING=ecs` adds the `aera-{env}-mirror` stack: the Mirror runs as one ECS
  Fargate task (in-memory SQLite, Cognito access tokens) behind an API Gateway HTTP API, and
  registers its URL and OAuth client with AERA by itself. Data returns to the reference
  scenario whenever the task restarts.
- The case service reserves one concurrent execution. New accounts may have a Lambda
  concurrency limit of 10, which refuses any reservation; set
  `AERA_CASE_SERVICE_RESERVED_CONCURRENCY=0` until the limit is raised.
- A running Docker engine is needed: CDK builds and publishes the ARM64 agent and optimizer
  images during deploy.

### Secrets

Secret containers are created empty under `/aera/{env}/...`. Values are placed at runtime:

```sh
make provision-secrets ENV=dev     # reads SAP_SANDBOX_API_KEY from your shell, never prints it
```

Channel secrets (`/aera/{env}/channels/whatsapp`, `/aera/{env}/channels/carrier-webhook`) are
set in Secrets Manager directly. Nothing secret is ever committed or passed as a command-line
argument.

## 4. Console

Build the console in [aera-fe](https://github.com/aera-scm/aera-fe) in live mode with the public
values from the stack outputs (`VITE_API_URL`, `VITE_WS_URL`, `VITE_USER_POOL_ID`,
`VITE_USER_POOL_CLIENT_ID`, `VITE_COGNITO_DOMAIN`), then upload it:

```sh
uv run --locked python scripts/deploy_console.py --env dev --profile <profile> --dist ../aera-fe/dist
```

Create users in the Cognito user pool and add them to the `planner`, `approver` or `admin`
group. There is no self sign-up.

## 5. Run the reference scenario

```sh
make replay-signals ENV=dev T0=<Mirror SCENARIO_T0>   # synthetic emails, WhatsApp photos, carrier events
aws lambda invoke --function-name aera-dev-mrp-poller --payload '{}' out.json   # poll MRP now
```

Within a few minutes the board shows six cases with the brake caliper housing first. Reset
the environment from the console (Settings → type `RESET dev`) to start again.

## 6. SAP Mirror on SAP BTP (optional)

```sh
make mirror-local                                             # local: SQLite, http://localhost:4004
node sap-mirror/scripts/fetch-sap-schemas.mjs && make mirror-model   # regenerate the model
cd sap-mirror && mbt build && cf deploy mta_archives/aera-sap-mirror_0.0.0.mtar
make register-mirror ENV=dev URL=https://<mirror-host>        # MIRROR_CLIENT_* in your shell
```

On BTP the Mirror uses XSUAA client credentials and SAP HANA Cloud (`mta.yaml`,
`xs-security.json`); dummy authentication exists only in the development profile. Read-only
sandbox and official-metadata checks are described in
[`sap-mirror/metadata/README.md`](../sap-mirror/metadata/README.md).

## 7. Hosted deployment from GitHub (optional)

`deploy-dev.yml` deploys from GitHub Actions with OIDC. It is disabled unless
`AERA_GITHUB_REPOSITORY` is set to the approved `owner/repository` and
`AERA_DEV_DEPLOY_ENABLED=true`. It also needs `AERA_CDK_QUALIFIER`, `AERA_BUDGET_NAME`,
`AERA_GITHUB_PROVIDER_MODE` (`create` or `existing`), `AERA_DEV_DEPLOY_ROLE_ARN` and a
customer CloudFormation execution policy (`AERA_CFN_EXECUTION_POLICY_ARN`) restricted to the
project's resources.

The trust policy permits only `repo:<repository>:ref:refs/heads/main`. The workflow runs only
after a successful CI run on `main`, checks out that exact commit, verifies the live budget and
then deploys. Pull-request validation has no cloud permissions.

## 8. Pause and teardown

**Pause** (keeps all data, idle cost drops to about USD 0.10 per day): set the Mirror ECS
service to 0 tasks and disable the `aera-{env}-mrp-poller` schedule and the
`aera-{env}-dialogue-sweep` and `aera-{env}-reliability-refresh` rules.

**Teardown** removes the environment:

```sh
pnpm exec cdk destroy --all --app "uv run --locked python -m infra.app" --profile <profile>
pnpm exec cdk destroy --app "uv run --locked python infra/budget_app.py" --profile <profile>
```

Stateful resources are retained on purpose and must be removed separately: the ten
DynamoDB tables (deletion protection on), the `raw`, `artefacts` and `audit` buckets (the
audit bucket uses S3 Object Lock in compliance mode — its objects cannot be deleted before
their 90-day retention ends), the KMS key (scheduled deletion, 7–30 days), the secrets, the
CDK bootstrap stack `CDKToolkit` with its asset bucket and ECR repository, and CloudWatch log
groups.
