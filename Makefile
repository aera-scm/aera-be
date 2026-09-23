.PHONY: setup lint typecheck test scan audit check hooks check-budget check-region check-models budget bootstrap deploy

# Deployment targets read AERA_AWS_PROFILE, AERA_REGION and AERA_BUDGET_* from the
# environment. Only ENV=dev is accepted; the budget is verified before bootstrap.
ENV ?= dev

setup:
	uv sync --locked
	pnpm install --frozen-lockfile

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
