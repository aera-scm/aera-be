.PHONY: types mirror-local mirror-model register-mirror setup lint typecheck test scan audit check hooks check-budget check-region check-models seed-config provision-secrets budget bootstrap deploy

# Deployment targets read AERA_AWS_PROFILE, AERA_REGION and AERA_BUDGET_* from the
# environment. Only ENV=dev is accepted; the budget is verified before bootstrap.
ENV ?= dev

setup:
	uv sync --locked
	pnpm install --frozen-lockfile
	pnpm --dir sap-mirror install --frozen-lockfile
	node sap-mirror/scripts/fetch-sap-schemas.mjs

lint:
	uv run --locked python scripts/check.py lint

typecheck:
	uv run --locked python scripts/check.py typecheck

test:
	uv run --locked python scripts/check.py test

scan:
	uv run --locked python scripts/security.py

audit:
	uv run --locked python scripts/check.py audit

check:
	uv run --locked python scripts/check.py

hooks:
	uv run --locked pre-commit run --all-files

check-region:
	uv run --locked python scripts/check_region.py $(ARGS)

check-models:
	uv run --locked python scripts/check_model_access.py $(ARGS)

check-budget:
	uv run --locked python scripts/check_budget.py

budget:
	uv run --locked python scripts/deploy_dev.py budget --env $(ENV)

bootstrap:
	uv run --locked python scripts/deploy_dev.py bootstrap --env $(ENV)

deploy:
	uv run --locked python scripts/deploy_dev.py deploy --env $(ENV)

seed-config:
	uv run --locked python scripts/seed_config.py --env $(ENV)

provision-secrets:
	uv run --locked python scripts/provision_secrets.py --env $(ENV)  # pragma: allowlist secret

# SAP Mirror on SQLite in memory, seeded with the reference scenario at start.
mirror-local:
	pnpm --dir sap-mirror start

# Regenerate the Mirror's S/4HANA entities from SAP's published schemas.
mirror-model:
	node sap-mirror/scripts/generate-model.mjs

# After `cf deploy`: store the Mirror URL in SSM and its OAuth client in Secrets Manager.
register-mirror:
	uv run --locked python scripts/register_mirror.py --env $(ENV) --url $(URL)  # pragma: allowlist secret

# Regenerate the console's API types from the shared Pydantic models.
FE ?= ../aera-fe
types:
	mkdir -p build
	uv run --locked python scripts/export_schemas.py > build/aera-contract.schema.json
	node scripts/generate-types.mjs build/aera-contract.schema.json $(FE)/src/api/types.generated.ts
