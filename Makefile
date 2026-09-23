.PHONY: setup lint typecheck test scan audit check hooks

setup:
	uv sync --locked

lint:
	uv run --locked python scripts/check.py lint

typecheck:
	uv run --locked python scripts/check.py typecheck

test:
	uv run --locked python scripts/check.py test

scan:
	uv run --locked python scripts/security.py

audit:
	uv run --locked pip-audit --local

check:
	uv run --locked python scripts/check.py

hooks:
	uv run --locked pre-commit run --all-files
