.PHONY: help dev up down logs api test fmt lint migrate revision seed

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

dev:  ## install backend deps into a local venv
	cd backend && uv sync

up:   ## start the full stack
	docker compose up -d --build

down: ## stop the stack
	docker compose down

logs: ## tail all logs
	docker compose logs -f --tail=100

api:  ## run the API locally against compose infra
	cd backend && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test: ## run the backend test suite
	cd backend && uv run pytest -q

fmt:  ## format
	cd backend && uv run ruff format . && uv run ruff check --fix .

lint: ## lint + typecheck
	cd backend && uv run ruff check . && uv run mypy app

migrate: ## apply migrations
	cd backend && uv run alembic upgrade head

revision: ## autogenerate a migration: make revision m="add x"
	cd backend && uv run alembic revision --autogenerate -m "$(m)"
