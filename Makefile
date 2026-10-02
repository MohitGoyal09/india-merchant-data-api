.PHONY: install fmt lint typecheck test test-live cov check cases cases-live agent-demo agent-evals serve backfill refresh canary docker-build docker-up mcp-evals demo-mcp smoke-compose security

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

# Against a running `make serve` on the real DB. Override with BASE_URL=http://host:port
BASE_URL ?= http://127.0.0.1:8000
cases-live:
	uv run python scripts/run_cases.py --base-url $(BASE_URL)

# Needs Claude credentials (ANTHROPIC_API_KEY or `ant auth login`). Opt-in: calls the real API.
# Runs on the recorded fixtures. make agent-demo Q="What is JPY 1,000 in INR on 24 Sep 2026?"
Q ?= Is 31 March 2026 a bank holiday in Mumbai?
agent-demo:
	uv run python scripts/agent_demo.py --fixtures "$(Q)"

# Layer-2 agent evals. `make agent-evals ARGS=--dry-run` needs no credentials.
agent-evals:
	uv run python evals/run_agent_evals.py $(ARGS)

serve:
	uv run imda serve

# Layer-1 MCP contract evals (no LLM, no network): must be 100%.
mcp-evals:
	uv run pytest tests/mcp

backfill:
	uv run imda backfill --from 2024-01-01

refresh:
	uv run imda refresh

canary:
	uv run imda canary

docker-build:
	docker build -t imda:dev .

docker-up:
	docker compose up --build -d api worker

# Keyless MCP demo: seeds a temp DB, starts `imda mcp` over stdio, runs ~20 asserted checks.
demo-mcp:
	uv run python scripts/demo_mcp.py

# Builds the image and exercises api + mcp under an isolated compose project. Needs Docker.
smoke-compose:
	bash scripts/smoke_compose.sh

# Static security scan (medium+ severity fails), known-vulnerability scan of the lockfile deps,
# and a secret scan of git-tracked files (fake test values go in .secrets-allowlist).
security:
	uv run bandit -r src -q -ll
	uv run pip-audit
	uv run python scripts/scan_secrets.py
