.PHONY: install fmt lint typecheck test test-live cov check cases serve backfill refresh canary

install:
	uv sync --quiet

fmt:
	uv run ruff format src tests scripts
	uv run ruff check --fix src tests scripts

lint:
	uv run ruff format --check src tests scripts
	uv run ruff check src tests scripts

typecheck:
	uv run mypy --pretty

test:
	uv run pytest -m "not live"

test-live:
	uv run pytest -m live

cov:
	uv run pytest -m "not live" --cov --cov-report=term

check: lint typecheck cov

cases:
	uv run python scripts/run_cases.py --offline

serve:
	uv run imda serve

backfill:
	uv run imda backfill --from 2024-01-01

refresh:
	uv run imda refresh

canary:
	uv run imda canary
