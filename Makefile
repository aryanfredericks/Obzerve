# Common development tasks. Requires uv, npm, and docker or podman.
COMPOSE ?= $(shell command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "podman compose")
ENGINE ?= $(shell command -v docker >/dev/null 2>&1 && echo docker || echo podman)
DEV_KEY ?= dev_project_key

.PHONY: help setup gpu-check db-up db-down migrate bootstrap server server-queue worker queue-status web demo loadtest test lint format ci sdk-check stack stack-demo stack-ps stack-logs stack-down

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

setup: ## Install Python (uv) and web (npm) dependencies
	uv sync --all-packages
	cd web && npm install

gpu-check: ## Verify PyTorch sees the GPU (ROCm build)
	uv run python -c "import torch; print('PyTorch:', torch.__version__); print('HIP:', torch.version.hip); print('GPU available:', torch.cuda.is_available()); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'Not detected')"

db-up: ## Start PostgreSQL
	$(COMPOSE) up -d

db-down: ## Stop PostgreSQL (data is kept in a volume)
	$(COMPOSE) down

migrate: ## Apply database migrations
	uv run obzerve-server migrate

bootstrap: migrate ## Migrate, create the "demo" project (dev API key), and price the demo model
	uv run obzerve-server create-project demo --key $(DEV_KEY) --if-not-exists
	uv run obzerve-server pricing set obzerve-mock-llm-1 --provider local --input 0.50 --output 1.50 \
		--cached-input 0.05 --note "Demo price for the mock model"

server: ## Run the API on :8000 with auto-reload (direct ingestion)
	uv run obzerve-server serve --reload

server-queue: ## Run the API on :8000 in queue mode (also run `make worker`)
	OBZERVE_INGEST_MODE=queue uv run obzerve-server serve --reload

worker: ## Run a queue worker (metrics on :9101; use WORKER_METRICS_PORT for a second one)
	OBZERVE_INGEST_MODE=queue OBZERVE_WORKER_METRICS_PORT=$${WORKER_METRICS_PORT:-9101} uv run obzerve-server worker

queue-status: ## Show queue backlog, in-progress, and dead-letter counts
	uv run obzerve-server queue status

web: ## Run the dashboard on :5173
	cd web && npm run dev

demo: ## Run the demo agent once per failure scenario
	@for fault in none llm-timeout bad-retrieval search-error llm-outage; do \
		echo "== $$fault"; uv run research-agent --fault $$fault --quiet || true; \
	done

loadtest: ## Send load for 20s and verify no telemetry was lost
	uv run python tools/loadtest.py --duration 20 --concurrency 16

test: ## Run all Python tests (server/queue tests need `make db-up`)
	uv run pytest

lint: ## Lint Python and typecheck the dashboard
	uv run ruff check .
	uv run ruff format --check .
	cd web && npm run typecheck

ci: ## Run the same checks as GitHub CI (needs `make db-up`)
	uv run ruff check .
	uv run ruff format --check .
	uv run obzerve-server migrate
	uv run obzerve-server check-migrations
	OBZERVE_REQUIRE_SERVICES=1 uv run pytest -rs
	cd web && npm run build

sdk-check: ## Build the SDK wheel, check its metadata, and test it installed on Python 3.10 and 3.13
	rm -rf dist && uv build --package obzerve -o dist
	uvx twine check --strict dist/*
	tools/test_sdk_wheel.sh 3.10 minimum dist/*.whl
	tools/test_sdk_wheel.sh 3.13 latest dist/*.whl

stack: ## Build and run the whole platform in containers (dashboard :8080, API :8000)
	$(COMPOSE) --profile stack up -d --build

stack-demo: ## Like `stack`, plus the demo project, its API key, and the demo model price
	OBZERVE_DASHBOARD_KEY=$(DEV_KEY) $(COMPOSE) --profile stack up -d --build
	$(COMPOSE) --profile stack exec api obzerve-server create-project demo --key $(DEV_KEY) --if-not-exists
	$(COMPOSE) --profile stack exec api obzerve-server pricing set obzerve-mock-llm-1 \
		--provider local --input 0.50 --output 1.50 --cached-input 0.05 --note "Demo price for the mock model"
	@echo "Dashboard: http://localhost:8080   API: http://localhost:8000   key: $(DEV_KEY)"

stack-ps: ## Show the platform's containers
	$(COMPOSE) --profile stack ps

stack-logs: ## Follow the API and worker logs
	$(COMPOSE) --profile stack logs -f api worker

stack-down: ## Stop all Obzerve containers, including scaled-out workers (data is kept)
	$(COMPOSE) --profile stack down
	@# `compose down` can miss replicas added with --scale; remove any that are left.
	@$(ENGINE) ps -aq --filter label=com.docker.compose.project=obzerve \
		--filter label=com.docker.compose.service=worker | xargs -r $(ENGINE) rm -f >/dev/null

format: ## Auto-format Python
	uv run ruff format .
	uv run ruff check --fix .
