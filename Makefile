# Convenience entry points. Every target is a thin wrapper over a command that
# also works standalone, so nothing here is required to run the project.
.DEFAULT_GOAL := help
.PHONY: help up down logs ps ingest test test-all lint fmt dev-api reset

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Start the full stack (db + ingest + api)
	docker compose up --build -d

down: ## Stop the stack, keeping data
	docker compose down

reset: ## Stop the stack and DELETE all data and the corpus cache
	docker compose down -v

logs: ## Follow API logs
	docker compose logs -f api

ps: ## Show service status
	docker compose ps

ingest: ## Re-run transcript ingestion
	docker compose run --rm ingest

test: ## Run tests that need no external services
	cd backend && .venv/Scripts/python -m pytest -m "not db and not llm"

test-all: ## Run every test, including those needing PostgreSQL
	cd backend && .venv/Scripts/python -m pytest

lint: ## Lint the backend
	cd backend && .venv/Scripts/python -m ruff check app tests

fmt: ## Format the backend
	cd backend && .venv/Scripts/python -m ruff format app tests

dev-api: ## Run the API on the host with reload
	cd backend && .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
